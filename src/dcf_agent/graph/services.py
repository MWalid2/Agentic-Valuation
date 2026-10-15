"""Live services for the graph: SEC, prices, FRED, Damodaran and the local model."""

from contextlib import ExitStack
from datetime import date, timedelta
from typing import Self

from dcf_agent.agents.answer import Answer, render
from dcf_agent.agents.data import DataAgent
from dcf_agent.agents.data_loop import DataLoop, DataReview
from dcf_agent.agents.lines import MappingStore
from dcf_agent.agents.router import Router
from dcf_agent.agents.snapshot import SnapshotAgent
from dcf_agent.agents.wacc import WaccAgent, WaccDecision
from dcf_agent.config import AppConfig
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import ValuationRequest
from dcf_agent.contracts.modeling import WACCResult
from dcf_agent.data.damodaran import DamodaranClient
from dcf_agent.data.fred import FREDClient
from dcf_agent.data.prices import PriceClient
from dcf_agent.data.sec import SECClient
from dcf_agent.graph.state import GraphState
from dcf_agent.graph.workflow import Services
from dcf_agent.llm import LocalLLM
from dcf_agent.tools.data import DataTools

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
        )

        def run() -> DataBundle:
            return DataAgent().run(
                tools,
                price_start=request.valuation_date - timedelta(days=730),
                industry=industry,
            )

        loop = DataLoop(self.llm, tools.mappings, online=not self.offline)
        return loop.run(run, request.plan, tools, self.sec)

    def wacc(self, bundle: DataBundle) -> tuple[WACCResult, WaccDecision | None]:
        agent = WaccAgent(self.prices.get_price_history, self.llm, marginal_tax=MARGINAL_TAX)
        return agent.run(bundle)

    def answer(self, state: GraphState) -> Answer:
        return render(state, self.llm)

    def services(self) -> Services:
        return Services(
            router=self.router, collect=self.collect, wacc=self.wacc, answer=self.answer
        )
