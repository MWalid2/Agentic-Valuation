"""Live services for the graph: SEC, prices, FRED, Damodaran and the local model."""

from contextlib import ExitStack
from datetime import date, timedelta
from typing import Self

from dcf_agent.agents.answer import Answer, render
from dcf_agent.agents.comps import CompsAgent
from dcf_agent.agents.critic import risk_passages, run_rubric
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
from dcf_agent.contracts.modeling import CompsResult, ForecastScenarios, WACCResult
from dcf_agent.contracts.workflow import CritiqueReport
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
        self,
        bundle: DataBundle,
        normalized: NormalizedFinancials,
        wacc: WACCResult,
        note: str | None = None,
    ) -> tuple[ForecastScenarios | None, tuple[str, ...]]:
        """Bear, base and bull from the Forecast Agent, with its notes."""
        assembled = assemble(bundle, marginal_tax=MARGINAL_TAX)
        result = ForecastAgent(self.llm).run(bundle, normalized, wacc, assembled, note)
        return result.scenarios, tuple(result.notes)

    def comps(
        self, bundle: DataBundle, dcf_value: float | None
    ) -> tuple[CompsResult, dict[str, float]]:
        """Peers, multiples and the implied range; the target's own figures for the Critic."""
        request = bundle.request
        if bundle.industry is None:
            raise ValueError("no Damodaran industry for the company")
        classification, _ = self.damodaran.get_classification()
        agent = CompsAgent(self.sec, self.prices, self.llm)
        result, _, _, target = agent.run(
            request.ticker,
            request.cik,
            bundle.industry.name,
            request.valuation_date,
            classification,
            dcf_value,
        )
        figures = {name: item.value for name, item in target.values.items()}
        # Period ends (as ordinals), so the Critic compares only like periods.
        for name, item in target.values.items():
            if item.prov.period_end is not None:
                figures[f"{name}_end"] = float(item.prov.period_end.toordinal())
        if result.implied_value_range is not None and dcf_value and result.divergence_vs_dcf:
            figures["implied_mid"] = dcf_value / (1 + result.divergence_vs_dcf)
        return result, figures

    def risks(self, bundle: DataBundle) -> list[tuple[str, str]]:
        """Risk-factor passages from the 10-K (accession, text), for the Critic's rubric."""
        request = bundle.request
        texts = filing_texts(self.sec, request.cik, request.valuation_date)
        annual = [t.accession for t in texts if t.form.startswith("10-K")]
        if not annual:
            return []
        index = self.index()
        for text in texts:
            index.add(text, request.cik)
        hits = index.search(
            "risk factors could adversely affect our business results of operations",
            annual,
            k=6,
        )
        return risk_passages([(f"accession {h.accession}", h.text) for h in hits])

    def rubric(self, state: GraphState, report: CritiqueReport) -> CritiqueReport:
        bundle = state.get("data")
        risks = self.risks(bundle) if bundle is not None else []
        return run_rubric(self.llm, dict(state), report, risks)

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
            comps=self.comps,
            rubric=self.rubric,
            models=self.config.settings.project_root / "outputs/models",
            risks=self.risks,
            reports=self.config.settings.project_root / "outputs/reports",
        )
