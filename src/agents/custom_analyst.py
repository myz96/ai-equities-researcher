import json

from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from src.graph.state import AgentState, show_agent_reasoning
from src.tools.api import get_company_news, get_financial_metrics
from src.utils.llm import call_llm
from src.utils.progress import progress


class CustomAnalystSignal(BaseModel):
    signal: str  # bullish | bearish | neutral
    confidence: float
    reasoning: str


def custom_analyst_agent(state: AgentState, agent_id: str, persona: dict):
    """A user-authored committee member.

    One generic engine plays any persona: it gathers the standard data
    bundle and lets the persona's philosophy drive the judgement.
    """
    data = state["data"]
    end_date = data["end_date"]
    tickers = data["tickers"]

    analysis = {}
    for ticker in tickers:
        progress.update_status(agent_id, ticker, "Gathering the data bundle")
        metrics = get_financial_metrics(ticker, end_date, period="ttm", limit=5)
        news = get_company_news(ticker, end_date, limit=8)

        metrics_payload = [m.model_dump(exclude_none=True) for m in metrics[:5]]
        headlines = [n.title for n in news[:8]]

        progress.update_status(agent_id, ticker, "Forming a view")
        output = generate_view(ticker, persona, metrics_payload, headlines, state, agent_id)

        analysis[ticker] = {
            "signal": output.signal.lower(),
            "confidence": max(0.0, min(100.0, output.confidence)),
            "reasoning": output.reasoning,
            "custom": True,
        }
        progress.update_status(agent_id, ticker, "Done")

    message = HumanMessage(content=json.dumps(analysis), name=agent_id)
    if state["metadata"]["show_reasoning"]:
        show_agent_reasoning(analysis, persona.get("name", agent_id))
    state["data"]["analyst_signals"][agent_id] = analysis
    return {"messages": [message], "data": data}


def generate_view(ticker, persona, metrics, headlines, state, agent_id) -> CustomAnalystSignal:
    template = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are {name} — {epithet} — a member of an investment committee.

Your investing philosophy, in your own words:
{philosophy}

Stay strictly in character. Judge the stock through your philosophy, not a
generic checklist. Be specific: cite the numbers or headlines that move you.
If your philosophy needs data that is not provided, say what you would look
at and lean on what is available.

Return JSON with exactly these fields:
- "signal": "bullish" | "bearish" | "neutral"
- "confidence": float between 0 and 100
- "reasoning": your view in your own voice, one tight paragraph""",
            ),
            (
                "human",
                """Ticker: {ticker}

Recent financial metrics (newest first):
{metrics}

Recent headlines:
{headlines}""",
            ),
        ]
    )
    prompt = template.invoke(
        {
            "name": persona.get("name", "Custom Analyst"),
            "epithet": persona.get("epithet") or "an independent investor",
            "philosophy": str(persona.get("philosophy", ""))[:6000],
            "ticker": ticker,
            "metrics": json.dumps(metrics, indent=1)[:6000],
            "headlines": json.dumps(headlines, indent=1)[:2000],
        }
    )

    def default_output():
        return CustomAnalystSignal(signal="neutral", confidence=0.0, reasoning="Error forming a view; abstaining.")

    return call_llm(prompt=prompt, pydantic_model=CustomAnalystSignal,
                    agent_name=agent_id, state=state, default_factory=default_output)
