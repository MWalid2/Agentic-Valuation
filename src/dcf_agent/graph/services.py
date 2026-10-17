"""Live services for the graph: SEC, prices, FRED, Damodaran and the local model."""

from contextlib import ExitStack
from datetime import date, timedelta
from typing import Self

from dcf_agent.agents.answer import Answer, render
from dcf_agent.agents.data import DataAgent
from dcf_agent.agents.data_loop import DataLoop, DataReview
from dcf_agent.agents.forecast import ForecastAgent
from dcf_agent.agents.lines import MappingStore
from dcf_agent.agents.normalizer import NormalizationAgent
from dcf_agent.agents.router import Router
from dcf_agent.agents.snapshot import SnapshotAgent
from dcf_agent.agents.wacc import WaccAgent, WaccDecision
from dcf_agent.config import AppConfig
from dcf_agent.contracts.accounting import NormalizedFinancials
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import ValuationRequest
from dcf_agent.contracts.modeling import ForecastScenarios, WACCResult
from dcf_agent.data.damodaran import DamodaranClient
from dcf_agent.data.fred import FREDClient
from dcf_agent.data.prices import PriceClient
from dcf_agent.data.sec import SECClient
from dcf_agent.engine.assemble import assemble
from dcf_agent.graph.state import GraphState
from dcf_agent.graph.workflow import Proposals, Services
from dcf_agent.llm import LocalLLM
from dcf_agent.rag.filings import filing_texts
from dcf_agent.rag.index import FilingIndex
from dcf_agent.tools.data import DataTools, periodic_filings

# US federal 21% plus a typical state rate (Damodaran's US marginal rate).
MARGINAL_TAX = 0.25


class LiveServices:
    """Opens every client once per session; use as a context manager."""

    def __init__(self, config: AppConfig, today: date, *, offline: bool = False) -> None:
        self.config, self.today, self.offline = config, today, offline
        self.stack = ExitStack()
        self.sec = self.stack.enter_context(SECClient(config, offline=offline))
        self.fred = self.stack.enter_context(FREDClient(config, offline=offline))
        self.damodaran = self.stack.enter_context(DamodaranClient(config, offline=offline))
        self.prices = self.stack.enter_context(PriceClient(config, offline=offline))
        self.llm = self.stack.enter_context(LocalLLM(config))
        self.router = Router(self.sec, today, self.llm)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.stack.close()

    def collect(
        self, request: ValuationRequest, industry: str | None
    ) -> tuple[DataBundle, DataReview | None]:
        tools = DataTools(
            request,
            sec=self.sec,
            snapshot_agent=SnapshotAgent(self.llm),
            fred=self.fred,
            damodaran=self.damodaran,
            prices=self.prices,
            mappings=MappingStore(self.config.settings.project_root / "data/mappings"),
            filings=self.index(),
        )

        def run() -> DataBundle:
            return DataAgent().run(
                tools,
                price_start=request.valuation_date - timedelta(days=730),
                industry=industry,
            )

        loop = DataLoop(self.llm, tools.mappings, online=not self.offline)
        return loop.run(run, request.plan, tools, self.sec)

    def index(self) -> FilingIndex:
        return FilingIndex(self.config.settings.project_root / "data/rag/index.sqlite")

    def normalize(self, bundle: DataBundle) -> Proposals:
        """The Normalization Agent's proposals for the bundle's history."""
        request = bundle.request
        if bundle.raw is None:
            return (), (), ("no raw financials",)
        as_of = request.valuation_date
        source, annual = periodic_filings(self.sec, request.cik, as_of, ("10-K",))
        if not annual:
            return (), (), ("no 10-K",)
        index = self.index()
        texts = filing_texts(self.sec, request.cik, as_of)
        for text in texts:
            index.add(text, request.cik)
        agent = NormalizationAgent(self.sec, index, self.llm)
        proposal = agent.propose(bundle.raw, source, annual, [t.accession for t in texts])
        proposals, flags = proposal.for_review()
        return proposals, flags, tuple(proposal.notes)

    def forecast(
        self, bundle: DataBundle, normalized: NormalizedFinancials, wacc: WACCResult
    ) -> tuple[ForecastScenarios | None, tuple[str, ...]]:
        """Bear, base and bull from the Forecast Agent, with its notes."""
        assembled = assemble(bundle, marginal_tax=MARGINAL_TAX)
        result = ForecastAgent(self.llm).run(bundle, normalized, wacc, assembled)
        return result.scenarios, tuple(result.notes)

    def wacc(self, bundle: DataBundle) -> tuple[WACCResult, WaccDecision | None]:
        agent = WaccAgent(self.prices.get_price_history, self.llm, marginal_tax=MARGINAL_TAX)
        return agent.run(bundle)

    def answer(self, state: GraphState) -> Answer:
        return render(state, self.llm)

    def services(self) -> Services:
        return Services(
            router=self.router,
            collect=self.collect,
            wacc=self.wacc,
            answer=self.answer,
            normalize=self.normalize,
            forecast=self.forecast,
            models=self.config.settings.project_root / "outputs/models",
        )
