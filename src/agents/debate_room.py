import json

from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from src.graph.state import AgentState, show_agent_reasoning
from src.utils.llm import call_llm
from src.utils.progress import progress


class Disagreement(BaseModel):
    topic: str
    bull_view: str
    bear_view: str


class DebateRoomOutput(BaseModel):
    signal: str  # bullish | bearish | neutral — the devil's advocate stance
    confidence: float
    devils_advocate: str
    disagreements: list[Disagreement]
    consensus_verdict: str


def debate_room_agent(state: AgentState, agent_id: str = "debate_room_agent"):
    """Adversarial review of the analyst signals.

    Runs after all analysts. For each ticker it identifies the majority view,
    argues the strongest case against it, and maps where the investors disagree.
    """
    data = state["data"]
    analyst_signals = data["analyst_signals"]
    tickers = data["tickers"]

    debate_analysis = {}

    for ticker in tickers:
        progress.update_status(agent_id, ticker, "Collecting analyst views")

        track_records = data.get("track_records") or {}
        views = []
        counts = {"bullish": 0, "bearish": 0, "neutral": 0}
        for agent, signals in analyst_signals.items():
            if agent.startswith(("risk_management", "debate_room")):
                continue
            if ticker not in signals:
                continue
            entry = signals[ticker]
            signal = str(entry.get("signal", "")).lower()
            if signal in counts:
                counts[signal] += 1
            reasoning = entry.get("reasoning", "")
            if isinstance(reasoning, dict):
                reasoning = json.dumps(reasoning)
            view = {
                "analyst": agent.replace("_agent", "").replace("_", " ").title(),
                "signal": signal,
                "confidence": entry.get("confidence"),
                "reasoning": str(reasoning)[:800],
            }
            record = track_records.get(agent.removesuffix("_agent"))
            if record:
                view["track_record"] = record
            views.append(view)

        if not views:
            progress.update_status(agent_id, ticker, "Failed: no analyst signals")
            continue

        majority = max(counts, key=counts.get)

        progress.update_status(agent_id, ticker, "Debating the consensus")
        output = generate_debate_output(ticker, views, counts, majority, state, agent_id)

        debate_analysis[ticker] = {
            "signal": output.signal,
            "confidence": output.confidence,
            "reasoning": output.devils_advocate,
            "disagreements": [d.model_dump() for d in output.disagreements],
            "consensus_verdict": output.consensus_verdict,
            "consensus": {**counts, "majority": majority},
        }

        progress.update_status(agent_id, ticker, "Done")

    message = HumanMessage(content=json.dumps(debate_analysis), name=agent_id)

    if state["metadata"]["show_reasoning"]:
        show_agent_reasoning(debate_analysis, "Debate Room")

    state["data"]["analyst_signals"][agent_id] = debate_analysis

    return {"messages": [message], "data": data}


def generate_debate_output(
    ticker: str,
    views: list[dict],
    counts: dict,
    majority: str,
    state: AgentState,
    agent_id: str,
) -> DebateRoomOutput:
    template = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are the Debate Room of an investment committee. A panel of famous
investors has given views on a stock. Your job is adversarial:

1. Play devil's advocate. Build the strongest, most specific case AGAINST the
   majority view, using real weaknesses in the panel's own arguments. Attack
   their assumptions, not straw men.
2. Map the genuine disagreements between panel members: where does a bull
   thesis directly contradict a bear thesis, and what would decide who is right?
3. Give a verdict on how robust the consensus is: is it independent analysis
   converging, or the same argument repeated?

Some panel members carry a "track_record" — their graded hit rate on past
calls. Use it as ammunition: an argument from a member with a poor record
deserves less deference, and say so by name.

Your `signal` is the stance of your devil's advocate case (if the majority is
bearish, you argue the bullish case, and vice versa; for a neutral majority,
argue whichever directional case is strongest). `confidence` (0-100) is how
much your counter-case should worry the committee.

Return JSON with exactly these fields:
- "signal": "bullish" | "bearish" | "neutral"
- "confidence": float between 0 and 100
- "devils_advocate": your case against the majority, cited to specific panel arguments
- "disagreements": list of {{"topic": ..., "bull_view": ..., "bear_view": ...}}
- "consensus_verdict": one paragraph on how robust the consensus is""",
            ),
            (
                "human",
                """Ticker: {ticker}

Vote count: {counts} — majority view: {majority}

Panel views:
{views}""",
            ),
        ]
    )

    prompt = template.invoke(
        {
            "ticker": ticker,
            "counts": json.dumps(counts),
            "majority": majority,
            "views": json.dumps(views, indent=2),
        }
    )

    def default_output():
        return DebateRoomOutput(
            signal="neutral",
            confidence=0.0,
            devils_advocate="Error generating debate; defaulting to no challenge.",
            disagreements=[],
            consensus_verdict="Unavailable.",
        )

    return call_llm(
        prompt=prompt,
        pydantic_model=DebateRoomOutput,
        agent_name=agent_id,
        state=state,
        default_factory=default_output,
    )
