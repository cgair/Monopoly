from typing import Optional
import datetime
import typer
import questionary
from pathlib import Path
from functools import wraps
from rich.console import Console
from rich.panel import Panel
from rich.spinner import Spinner
from rich.live import Live
from rich.columns import Columns
from rich.markdown import Markdown
from rich.layout import Layout
from rich.text import Text
from rich.table import Table
from collections import deque
import time
from rich.tree import Tree
from rich import box
from rich.align import Align
from rich.rule import Rule

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.graph.analyst_execution import (
    AnalystWallTimeTracker,
    build_analyst_execution_plan,
    get_initial_analyst_node,
    sync_analyst_tracker_from_chunk,
)
from tradingagents.default_config import DEFAULT_CONFIG
from cli.models import AnalystType
from cli.utils import *
from cli.announcements import fetch_announcements, display_announcements
from cli.stats_handler import StatsCallbackHandler

console = Console()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


# Create a deque to store recent messages with a maximum length
class MessageBuffer:
    # Fixed teams that always run (not user-selectable)
    FIXED_AGENTS = {
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # Analyst name mapping
    ANALYST_MAPPING = {
        "market": "Market Analyst",
        "social": "Sentiment Analyst",
        "news": "News Analyst",
    }

    # Report section mapping: section -> (analyst_key for filtering, finalizing_agent)
    # analyst_key: which analyst selection controls this section (None = always included)
    # finalizing_agent: which agent must be "completed" for this report to count as done
    REPORT_SECTIONS = {
        "market_report": ("market", "Market Analyst"),
        "sentiment_report": ("social", "Sentiment Analyst"),
        "news_report": ("news", "News Analyst"),
        "investment_plan": (None, "Research Manager"),
        "trader_investment_plan": (None, "Trader"),
        "final_trade_decision": (None, "Portfolio Manager"),
    }

    def __init__(self, max_length=100):
        self.messages = deque(maxlen=max_length)
        self.tool_calls = deque(maxlen=max_length)
        self.current_report = None
        self.final_report = None  # Store the complete final report
        self.agent_status = {}
        self.current_agent = None
        self.report_sections = {}
        self.selected_analysts = []
        self._processed_message_ids = set()

    def init_for_analysis(self, selected_analysts):
        """Initialize agent status and report sections based on selected analysts.

        Args:
            selected_analysts: List of analyst type strings (e.g., ["market", "news"])
        """
        self.selected_analysts = [a.lower() for a in selected_analysts]

        # Build agent_status dynamically
        self.agent_status = {}

        # Add selected analysts
        for analyst_key in self.selected_analysts:
            if analyst_key in self.ANALYST_MAPPING:
                self.agent_status[self.ANALYST_MAPPING[analyst_key]] = "pending"

        # Add fixed teams
        for team_agents in self.FIXED_AGENTS.values():
            for agent in team_agents:
                self.agent_status[agent] = "pending"

        # Build report_sections dynamically
        self.report_sections = {}
        for section, (analyst_key, _) in self.REPORT_SECTIONS.items():
            if analyst_key is None or analyst_key in self.selected_analysts:
                self.report_sections[section] = None

        # Reset other state
        self.current_report = None
        self.final_report = None
        self.current_agent = None
        self.messages.clear()
        self.tool_calls.clear()
        self._processed_message_ids.clear()

    def get_completed_reports_count(self):
        """Count reports that are finalized (their finalizing agent is completed).

        A report is considered complete when:
        1. The report section has content (not None), AND
        2. The agent responsible for finalizing that report has status "completed"

        This prevents interim updates (like debate rounds) from counting as completed.
        """
        count = 0
        for section in self.report_sections:
            if section not in self.REPORT_SECTIONS:
                continue
            _, finalizing_agent = self.REPORT_SECTIONS[section]
            # Report is complete if it has content AND its finalizing agent is done
            has_content = self.report_sections.get(section) is not None
            agent_done = self.agent_status.get(finalizing_agent) == "completed"
            if has_content and agent_done:
                count += 1
        return count

    def add_message(self, message_type, content):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.messages.append((timestamp, message_type, content))

    def add_tool_call(self, tool_name, args):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.tool_calls.append((timestamp, tool_name, args))

    def update_agent_status(self, agent, status):
        if agent in self.agent_status:
            self.agent_status[agent] = status
            self.current_agent = agent

    def update_report_section(self, section_name, content):
        if section_name in self.report_sections:
            self.report_sections[section_name] = content
            self._update_current_report()

    def _update_current_report(self):
        # For the panel display, only show the most recently updated section
        latest_section = None
        latest_content = None

        # Find the most recently updated section
        for section, content in self.report_sections.items():
            if content is not None:
                latest_section = section
                latest_content = content
               
        if latest_section and latest_content:
            # Format the current section for display
            section_titles = {
                "market_report": "Market Analysis",
                "sentiment_report": "Social Sentiment",
                "news_report": "News Analysis",
                "investment_plan": "Research Team Decision",
                "trader_investment_plan": "Trading Team Plan",
                "final_trade_decision": "Portfolio Management Decision",
            }
            self.current_report = (
                f"### {section_titles[latest_section]}\n{latest_content}"
            )

        # Update the final complete report
        self._update_final_report()

    def _update_final_report(self):
        report_parts = []

        # Analyst Team Reports - use .get() to handle missing sections
        analyst_sections = ["market_report", "sentiment_report", "news_report"]
        if any(self.report_sections.get(section) for section in analyst_sections):
            report_parts.append("## Analyst Team Reports")
            if self.report_sections.get("market_report"):
                report_parts.append(
                    f"### Market Analysis\n{self.report_sections['market_report']}"
                )
            if self.report_sections.get("sentiment_report"):
                report_parts.append(
                    f"### Social Sentiment\n{self.report_sections['sentiment_report']}"
                )
            if self.report_sections.get("news_report"):
                report_parts.append(
                    f"### News Analysis\n{self.report_sections['news_report']}"
                )

        # Research Team Reports
        if self.report_sections.get("investment_plan"):
            report_parts.append("## Research Team Decision")
            report_parts.append(f"{self.report_sections['investment_plan']}")

        # Trading Team Reports
        if self.report_sections.get("trader_investment_plan"):
            report_parts.append("## Trading Team Plan")
            report_parts.append(f"{self.report_sections['trader_investment_plan']}")

        # Portfolio Management Decision
        if self.report_sections.get("final_trade_decision"):
            report_parts.append("## Portfolio Management Decision")
            report_parts.append(f"{self.report_sections['final_trade_decision']}")

        self.final_report = "\n\n".join(report_parts) if report_parts else None


message_buffer = MessageBuffer()


def create_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )
    layout["main"].split_column(
        Layout(name="upper", ratio=3), Layout(name="analysis", ratio=5)
    )
    layout["upper"].split_row(
        Layout(name="progress", ratio=2), Layout(name="messages", ratio=3)
    )
    return layout


def format_tokens(n):
    """Format token count for display."""
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def update_display(layout, spinner_text=None, stats_handler=None, start_time=None):
    # Header with welcome message
    layout["header"].update(
        Panel(
            "[bold green]Welcome to TradingAgents CLI[/bold green]\n"
            "[dim]© [Tauric Research](https://github.com/TauricResearch)[/dim]",
            title="Welcome to TradingAgents",
            border_style="green",
            padding=(1, 2),
            expand=True,
        )
    )

    # Progress panel showing agent status
    progress_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        box=box.SIMPLE_HEAD,  # Use simple header with horizontal lines
        title=None,  # Remove the redundant Progress title
        padding=(0, 2),  # Add horizontal padding
        expand=True,  # Make table expand to fill available space
    )
    progress_table.add_column("Team", style="cyan", justify="center", width=20)
    progress_table.add_column("Agent", style="green", justify="center", width=20)
    progress_table.add_column("Status", style="yellow", justify="center", width=20)

    # Group agents by team - filter to only include agents in agent_status
    all_teams = {
        "Analyst Team": [
            "Market Analyst",
            "Sentiment Analyst",
            "News Analyst",
            "Fundamentals Analyst",
        ],
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # Filter teams to only include agents that are in agent_status
    teams = {}
    for team, agents in all_teams.items():
        active_agents = [a for a in agents if a in message_buffer.agent_status]
        if active_agents:
            teams[team] = active_agents

    for team, agents in teams.items():
        # Add first agent with team name
        first_agent = agents[0]
        status = message_buffer.agent_status.get(first_agent, "pending")
        if status == "in_progress":
            spinner = Spinner(
                "dots", text="[blue]in_progress[/blue]", style="bold cyan"
            )
            status_cell = spinner
        else:
            status_color = {
                "pending": "yellow",
                "completed": "green",
                "error": "red",
            }.get(status, "white")
            status_cell = f"[{status_color}]{status}[/{status_color}]"
        progress_table.add_row(team, first_agent, status_cell)

        # Add remaining agents in team
        for agent in agents[1:]:
            status = message_buffer.agent_status.get(agent, "pending")
            if status == "in_progress":
                spinner = Spinner(
                    "dots", text="[blue]in_progress[/blue]", style="bold cyan"
                )
                status_cell = spinner
            else:
                status_color = {
                    "pending": "yellow",
                    "completed": "green",
                    "error": "red",
                }.get(status, "white")
                status_cell = f"[{status_color}]{status}[/{status_color}]"
            progress_table.add_row("", agent, status_cell)

        # Add horizontal line after each team
        progress_table.add_row("─" * 20, "─" * 20, "─" * 20, style="dim")

    layout["progress"].update(
        Panel(progress_table, title="Progress", border_style="cyan", padding=(1, 2))
    )

    # Messages panel showing recent messages and tool calls
    messages_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        expand=True,  # Make table expand to fill available space
        box=box.MINIMAL,  # Use minimal box style for a lighter look
        show_lines=True,  # Keep horizontal lines
        padding=(0, 1),  # Add some padding between columns
    )
    messages_table.add_column("Time", style="cyan", width=8, justify="center")
    messages_table.add_column("Type", style="green", width=10, justify="center")
    messages_table.add_column(
        "Content", style="white", no_wrap=False, ratio=1
    )  # Make content column expand

    # Combine tool calls and messages
    all_messages = []

    # Add tool calls
    for timestamp, tool_name, args in message_buffer.tool_calls:
        formatted_args = format_tool_args(args)
        all_messages.append((timestamp, "Tool", f"{tool_name}: {formatted_args}"))

    # Add regular messages
    for timestamp, msg_type, content in message_buffer.messages:
        content_str = str(content) if content else ""
        if len(content_str) > 200:
            content_str = content_str[:197] + "..."
        all_messages.append((timestamp, msg_type, content_str))

    # Sort by timestamp descending (newest first)
    all_messages.sort(key=lambda x: x[0], reverse=True)

    # Calculate how many messages we can show based on available space
    max_messages = 12

    # Get the first N messages (newest ones)
    recent_messages = all_messages[:max_messages]

    # Add messages to table (already in newest-first order)
    for timestamp, msg_type, content in recent_messages:
        # Format content with word wrapping
        wrapped_content = Text(content, overflow="fold")
        messages_table.add_row(timestamp, msg_type, wrapped_content)

    layout["messages"].update(
        Panel(
            messages_table,
            title="Messages & Tools",
            border_style="blue",
            padding=(1, 2),
        )
    )

    # Analysis panel showing current report
    if message_buffer.current_report:
        layout["analysis"].update(
            Panel(
                Markdown(message_buffer.current_report),
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )
    else:
        layout["analysis"].update(
            Panel(
                "[italic]Waiting for analysis report...[/italic]",
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )

    # Footer with statistics
    # Agent progress - derived from agent_status dict
    agents_completed = sum(
        1 for status in message_buffer.agent_status.values() if status == "completed"
    )
    agents_total = len(message_buffer.agent_status)

    # Report progress - based on agent completion (not just content existence)
    reports_completed = message_buffer.get_completed_reports_count()
    reports_total = len(message_buffer.report_sections)

    # Build stats parts
    stats_parts = [f"Agents: {agents_completed}/{agents_total}"]

    # LLM and tool stats from callback handler
    if stats_handler:
        stats = stats_handler.get_stats()
        stats_parts.append(f"LLM: {stats['llm_calls']}")
        stats_parts.append(f"Tools: {stats['tool_calls']}")

        # Token display with graceful fallback
        if stats["tokens_in"] > 0 or stats["tokens_out"] > 0:
            tokens_str = f"Tokens: {format_tokens(stats['tokens_in'])}\u2191 {format_tokens(stats['tokens_out'])}\u2193"
        else:
            tokens_str = "Tokens: --"
        stats_parts.append(tokens_str)

    stats_parts.append(f"Reports: {reports_completed}/{reports_total}")

    # Elapsed time
    if start_time:
        elapsed = time.time() - start_time
        elapsed_str = f"\u23f1 {int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        stats_parts.append(elapsed_str)

    stats_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    stats_table.add_column("Stats", justify="center")
    stats_table.add_row(" | ".join(stats_parts))

    layout["footer"].update(Panel(stats_table, border_style="grey50"))


def get_user_selections():
    """Get all user selections before starting the analysis display."""
    # Display ASCII art welcome message
    with open(Path(__file__).parent / "static" / "welcome.txt", "r", encoding="utf-8") as f:
        welcome_ascii = f.read()

    # Create welcome box content
    welcome_content = f"{welcome_ascii}\n"
    welcome_content += "[bold green]TradingAgents: Multi-Agents LLM Financial Trading Framework - CLI[/bold green]\n\n"
    welcome_content += "[bold]Workflow Steps:[/bold]\n"
    welcome_content += "I. Analyst Team → II. Research Team → III. Trader → IV. Risk Management → V. Portfolio Management\n\n"
    welcome_content += (
        "[dim]Built by [Tauric Research](https://github.com/TauricResearch)[/dim]"
    )

    # Create and center the welcome box
    welcome_box = Panel(
        welcome_content,
        border_style="green",
        padding=(1, 2),
        title="Welcome to TradingAgents",
        subtitle="Multi-Agents LLM Financial Trading Framework",
    )
    console.print(Align.center(welcome_box))
    console.print()
    console.print()  # Add vertical space before announcements

    # Fetch and display announcements (silent on failure)
    announcements = fetch_announcements()
    display_announcements(console, announcements)

    # Create a boxed questionnaire for each step
    def create_question_box(title, prompt, default=None):
        box_content = f"[bold]{title}[/bold]\n"
        box_content += f"[dim]{prompt}[/dim]"
        if default:
            box_content += f"\n[dim]Default: {default}[/dim]"
        return Panel(box_content, border_style="blue", padding=(1, 2))

    # Step 1: Ticker symbol
    console.print(
        create_question_box(
            "Step 1: Ticker Symbol",
            "Enter the exact crypto perp ticker to analyze (examples: BTC-USD, ETH-USD, BTCUSDT)",
            "BTC-USD",
        )
    )
    selected_ticker = get_ticker()

    # Step 2: Analysis date
    default_date = datetime.datetime.now().strftime("%Y-%m-%d")
    console.print(
        create_question_box(
            "Step 2: Analysis Date",
            "Enter the analysis date (YYYY-MM-DD)",
            default_date,
        )
    )
    analysis_date = get_analysis_date()

    # Step 3: Output language
    console.print(
        create_question_box(
            "Step 3: Output Language",
            "Select the language for analyst reports and final decision"
        )
    )
    output_language = ask_output_language()

    # Step 4: Select analysts
    console.print(
        create_question_box(
            "Step 4: Analysts Team", "Select your LLM analyst agents for the analysis"
        )
    )
    selected_analysts = select_analysts()
    console.print(
        f"[green]Selected analysts:[/green] {', '.join(analyst.value for analyst in selected_analysts)}"
    )

    # Step 5: Research depth
    console.print(
        create_question_box(
            "Step 5: Research Depth", "Select your research depth level"
        )
    )
    selected_research_depth = select_research_depth()

    # Step 6: LLM Provider
    console.print(
        create_question_box(
            "Step 6: LLM Provider", "Select your LLM provider"
        )
    )
    selected_llm_provider, backend_url = select_llm_provider()

    # Providers with regional endpoints prompt for the region as a secondary
    # step so the main dropdown stays clean (mainland China and international
    # accounts cannot share API keys).
    if selected_llm_provider == "qwen":
        selected_llm_provider, backend_url = ask_qwen_region()
    elif selected_llm_provider == "minimax":
        selected_llm_provider, backend_url = ask_minimax_region()
    elif selected_llm_provider == "glm":
        selected_llm_provider, backend_url = ask_glm_region()

    # For Ollama, surface the resolved endpoint (OLLAMA_BASE_URL vs default)
    # before model selection so it's obvious where we're connecting.
    if selected_llm_provider == "ollama":
        confirm_ollama_endpoint(backend_url)

    # Confirm the provider's API key is present; prompt the user to paste
    # one and persist it to .env if it's missing, so the analysis run
    # doesn't fail later at the first API call.
    ensure_api_key(selected_llm_provider)

    # Step 7: Thinking agents
    console.print(
        create_question_box(
            "Step 7: Thinking Agents", "Select your thinking agents for analysis"
        )
    )
    selected_shallow_thinker = select_shallow_thinking_agent(selected_llm_provider)
    selected_deep_thinker = select_deep_thinking_agent(selected_llm_provider)

    # Step 8: Provider-specific thinking configuration
    thinking_level = None
    reasoning_effort = None
    anthropic_effort = None

    provider_lower = selected_llm_provider.lower()
    if provider_lower == "google":
        console.print(
            create_question_box(
                "Step 8: Thinking Mode",
                "Configure Gemini thinking mode"
            )
        )
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        console.print(
            create_question_box(
                "Step 8: Reasoning Effort",
                "Configure OpenAI reasoning effort level"
            )
        )
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        console.print(
            create_question_box(
                "Step 8: Effort Level",
                "Configure Claude effort level"
            )
        )
        anthropic_effort = ask_anthropic_effort()

    return {
        "ticker": selected_ticker,
        "analysis_date": analysis_date,
        "analysts": selected_analysts,
        "research_depth": selected_research_depth,
        "llm_provider": selected_llm_provider.lower(),
        "backend_url": backend_url,
        "shallow_thinker": selected_shallow_thinker,
        "deep_thinker": selected_deep_thinker,
        "google_thinking_level": thinking_level,
        "openai_reasoning_effort": reasoning_effort,
        "anthropic_effort": anthropic_effort,
        "output_language": output_language,
    }


def get_ticker():
    """Get a crypto perp ticker from user input, preserving the quote suffix."""
    # typer.prompt strips trailing dot-suffixes on some shells; questionary.text
    # reads the raw line.
    ticker = questionary.text(
        "",
        validate=lambda value: (
            not value.strip()
            or validate_crypto_ticker(value)
        )
        or "Only crypto perp symbols are supported, e.g. BTC-USD, ETH-USD, BTCUSDT.",
    ).ask()

    if ticker is None:
        console.print("\n[red]No ticker symbol provided. Exiting...[/red]")
        raise typer.Exit(1)

    return (ticker.strip() or "BTC-USD").upper()


def get_analysis_date():
    """Get the analysis date from user input."""
    while True:
        date_str = typer.prompt(
            "", default=datetime.datetime.now().strftime("%Y-%m-%d")
        )
        try:
            # Validate date format and ensure it's not in the future
            analysis_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            if analysis_date.date() > datetime.datetime.now().date():
                console.print("[red]Error: Analysis date cannot be in the future[/red]")
                continue
            return date_str
        except ValueError:
            console.print(
                "[red]Error: Invalid date format. Please use YYYY-MM-DD[/red]"
            )


def save_report_to_disk(final_state, ticker: str, save_path: Path):
    """Save complete analysis report to disk with organized subfolders."""
    save_path.mkdir(parents=True, exist_ok=True)
    sections = []

    # 1. Analysts
    analysts_dir = save_path / "1_analysts"
    analyst_parts = []
    if final_state.get("market_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "market.md").write_text(final_state["market_report"], encoding="utf-8")
        analyst_parts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "sentiment.md").write_text(final_state["sentiment_report"], encoding="utf-8")
        analyst_parts.append(("Sentiment Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "news.md").write_text(final_state["news_report"], encoding="utf-8")
        analyst_parts.append(("News Analyst", final_state["news_report"]))
    if analyst_parts:
        content = "\n\n".join(f"### {name}\n{text}" for name, text in analyst_parts)
        sections.append(f"## I. Analyst Team Reports\n\n{content}")

    # 2. Research
    if final_state.get("investment_debate_state"):
        research_dir = save_path / "2_research"
        debate = final_state["investment_debate_state"]
        research_parts = []
        if debate.get("bull_history"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "bull.md").write_text(debate["bull_history"], encoding="utf-8")
            research_parts.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "bear.md").write_text(debate["bear_history"], encoding="utf-8")
            research_parts.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "manager.md").write_text(debate["judge_decision"], encoding="utf-8")
            research_parts.append(("Research Manager", debate["judge_decision"]))
        if research_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in research_parts)
            sections.append(f"## II. Research Team Decision\n\n{content}")

    # 3. Trading
    if final_state.get("trader_investment_plan"):
        trading_dir = save_path / "3_trading"
        trading_dir.mkdir(exist_ok=True)
        (trading_dir / "trader.md").write_text(final_state["trader_investment_plan"], encoding="utf-8")
        sections.append(f"## III. Trading Team Plan\n\n### Trader\n{final_state['trader_investment_plan']}")

    # 4. Risk Management
    if final_state.get("risk_debate_state"):
        risk_dir = save_path / "4_risk"
        risk = final_state["risk_debate_state"]
        risk_parts = []
        if risk.get("aggressive_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "aggressive.md").write_text(risk["aggressive_history"], encoding="utf-8")
            risk_parts.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "conservative.md").write_text(risk["conservative_history"], encoding="utf-8")
            risk_parts.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "neutral.md").write_text(risk["neutral_history"], encoding="utf-8")
            risk_parts.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in risk_parts)
            sections.append(f"## IV. Risk Management Team Decision\n\n{content}")

        # 5. Portfolio Manager
        if risk.get("judge_decision"):
            portfolio_dir = save_path / "5_portfolio"
            portfolio_dir.mkdir(exist_ok=True)
            (portfolio_dir / "decision.md").write_text(risk["judge_decision"], encoding="utf-8")
            sections.append(f"## V. Portfolio Manager Decision\n\n### Portfolio Manager\n{risk['judge_decision']}")

    # Write consolidated report
    header = f"# Trading Analysis Report: {ticker}\n\nGenerated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    (save_path / "complete_report.md").write_text(header + "\n\n".join(sections), encoding="utf-8")
    return save_path / "complete_report.md"


def display_complete_report(final_state):
    """Display the complete analysis report sequentially (avoids truncation)."""
    console.print()
    console.print(Rule("Complete Analysis Report", style="bold green"))

    # I. Analyst Team Reports
    analysts = []
    if final_state.get("market_report"):
        analysts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts.append(("Sentiment Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts.append(("News Analyst", final_state["news_report"]))
    if analysts:
        console.print(Panel("[bold]I. Analyst Team Reports[/bold]", border_style="cyan"))
        for title, content in analysts:
            console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # II. Research Team Reports
    if final_state.get("investment_debate_state"):
        debate = final_state["investment_debate_state"]
        research = []
        if debate.get("bull_history"):
            research.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research.append(("Research Manager", debate["judge_decision"]))
        if research:
            console.print(Panel("[bold]II. Research Team Decision[/bold]", border_style="magenta"))
            for title, content in research:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # III. Trading Team
    if final_state.get("trader_investment_plan"):
        console.print(Panel("[bold]III. Trading Team Plan[/bold]", border_style="yellow"))
        console.print(Panel(Markdown(final_state["trader_investment_plan"]), title="Trader", border_style="blue", padding=(1, 2)))

    # IV. Risk Management Team
    if final_state.get("risk_debate_state"):
        risk = final_state["risk_debate_state"]
        risk_reports = []
        if risk.get("aggressive_history"):
            risk_reports.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_reports.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_reports.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_reports:
            console.print(Panel("[bold]IV. Risk Management Team Decision[/bold]", border_style="red"))
            for title, content in risk_reports:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

        # V. Portfolio Manager Decision
        if risk.get("judge_decision"):
            console.print(Panel("[bold]V. Portfolio Manager Decision[/bold]", border_style="green"))
            console.print(Panel(Markdown(risk["judge_decision"]), title="Portfolio Manager", border_style="blue", padding=(1, 2)))


def update_research_team_status(status):
    """Update status for research team members (not Trader)."""
    research_team = ["Bull Researcher", "Bear Researcher", "Research Manager"]
    for agent in research_team:
        message_buffer.update_agent_status(agent, status)


# Ordered list of analysts for status transitions
ANALYST_ORDER = ["market", "social", "news"]
ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Sentiment Analyst",
    "news": "News Analyst",
}
ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
}


def update_analyst_statuses(message_buffer, chunk, wall_time_tracker=None):
    """Update analyst statuses based on accumulated report state.

    Logic:
    - Store new report content from the current chunk if present
    - Check accumulated report_sections (not just current chunk) for status
    - Analysts with reports = completed
    - First analyst without report = in_progress
    - Remaining analysts without reports = pending
    - When all analysts done, set Bull Researcher to in_progress
    """
    selected = message_buffer.selected_analysts
    found_active = False

    if wall_time_tracker is not None:
        sync_analyst_tracker_from_chunk(wall_time_tracker, chunk)

    for analyst_key in ANALYST_ORDER:
        if analyst_key not in selected:
            continue

        agent_name = ANALYST_AGENT_NAMES[analyst_key]
        report_key = ANALYST_REPORT_MAP[analyst_key]

        # Capture new report content from current chunk
        if chunk.get(report_key):
            message_buffer.update_report_section(report_key, chunk[report_key])

        # Determine status from accumulated sections, not just current chunk
        has_report = bool(message_buffer.report_sections.get(report_key))

        if has_report:
            message_buffer.update_agent_status(agent_name, "completed")
        elif not found_active:
            message_buffer.update_agent_status(agent_name, "in_progress")
            found_active = True
        else:
            message_buffer.update_agent_status(agent_name, "pending")

    # When all analysts complete, transition research team to in_progress
    if not found_active and selected:
        if message_buffer.agent_status.get("Bull Researcher") == "pending":
            message_buffer.update_agent_status("Bull Researcher", "in_progress")

def extract_content_string(content):
    """Extract string content from various message formats.
    Returns None if no meaningful text content is found.
    """
    import ast

    def is_empty(val):
        """Check if value is empty using Python's truthiness."""
        if val is None or val == '':
            return True
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return True
            try:
                return not bool(ast.literal_eval(s))
            except (ValueError, SyntaxError):
                return False  # Can't parse = real text
        return not bool(val)

    if is_empty(content):
        return None

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, dict):
        text = content.get('text', '')
        return text.strip() if not is_empty(text) else None

    if isinstance(content, list):
        text_parts = [
            item.get('text', '').strip() if isinstance(item, dict) and item.get('type') == 'text'
            else (item.strip() if isinstance(item, str) else '')
            for item in content
        ]
        result = ' '.join(t for t in text_parts if t and not is_empty(t))
        return result if result else None

    return str(content).strip() if not is_empty(content) else None


def classify_message_type(message) -> tuple[str, str | None]:
    """Classify LangChain message into display type and extract content.

    Returns:
        (type, content) - type is one of: User, Agent, Data, Control
                        - content is extracted string or None
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    content = extract_content_string(getattr(message, 'content', None))

    if isinstance(message, HumanMessage):
        if content and content.strip() == "Continue":
            return ("Control", content)
        return ("User", content)

    if isinstance(message, ToolMessage):
        return ("Data", content)

    if isinstance(message, AIMessage):
        return ("Agent", content)

    # Fallback for unknown types
    return ("System", content)


def format_tool_args(args, max_length=80) -> str:
    """Format tool arguments for terminal display."""
    result = str(args)
    if len(result) > max_length:
        return result[:max_length - 3] + "..."
    return result

def run_analysis(checkpoint: bool = False):
    # First get all user selections
    selections = get_user_selections()

    # Create config with selected research depth
    config = DEFAULT_CONFIG.copy()
    config["max_debate_rounds"] = selections["research_depth"]
    config["max_risk_discuss_rounds"] = selections["research_depth"]
    config["quick_think_llm"] = selections["shallow_thinker"]
    config["deep_think_llm"] = selections["deep_thinker"]
    config["backend_url"] = selections["backend_url"]
    config["llm_provider"] = selections["llm_provider"].lower()
    # Provider-specific thinking configuration
    config["google_thinking_level"] = selections.get("google_thinking_level")
    config["openai_reasoning_effort"] = selections.get("openai_reasoning_effort")
    config["anthropic_effort"] = selections.get("anthropic_effort")
    config["output_language"] = selections.get("output_language", "English")
    config["checkpoint_enabled"] = checkpoint

    # Create stats callback handler for tracking LLM/tool calls
    stats_handler = StatsCallbackHandler()

    # Normalize analyst selection to predefined order (selection is a 'set', order is fixed)
    selected_set = {analyst.value for analyst in selections["analysts"]}
    selected_analyst_keys = [a for a in ANALYST_ORDER if a in selected_set]
    analyst_execution_plan = build_analyst_execution_plan(
        selected_analyst_keys,
        concurrency_limit=config["analyst_concurrency_limit"],
    )
    analyst_wall_time_tracker = AnalystWallTimeTracker(analyst_execution_plan)

    # Initialize the graph with callbacks bound to LLMs
    graph = TradingAgentsGraph(
        selected_analyst_keys,
        config=config,
        debug=True,
        callbacks=[stats_handler],
    )

    # Initialize message buffer with selected analysts
    message_buffer.init_for_analysis(selected_analyst_keys)

    # Track start time for elapsed display
    start_time = time.time()

    # Create result directory
    results_dir = Path(config["results_dir"]) / selections["ticker"] / selections["analysis_date"]
    results_dir.mkdir(parents=True, exist_ok=True)
    report_dir = results_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    log_file = results_dir / "message_tool.log"
    log_file.touch(exist_ok=True)

    def save_message_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, message_type, content = obj.messages[-1]
            content = content.replace("\n", " ")  # Replace newlines with spaces
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [{message_type}] {content}\n")
        return wrapper
    
    def save_tool_call_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, tool_name, args = obj.tool_calls[-1]
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [Tool Call] {tool_name}({args_str})\n")
        return wrapper

    def save_report_section_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(section_name, content):
            func(section_name, content)
            if section_name in obj.report_sections and obj.report_sections[section_name] is not None:
                content = obj.report_sections[section_name]
                if content:
                    file_name = f"{section_name}.md"
                    text = "\n".join(str(item) for item in content) if isinstance(content, list) else content
                    with open(report_dir / file_name, "w", encoding="utf-8") as f:
                        f.write(text)
        return wrapper

    message_buffer.add_message = save_message_decorator(message_buffer, "add_message")
    message_buffer.add_tool_call = save_tool_call_decorator(message_buffer, "add_tool_call")
    message_buffer.update_report_section = save_report_section_decorator(message_buffer, "update_report_section")

    # Now start the display layout
    layout = create_layout()

    with Live(layout, refresh_per_second=4) as live:
        # Initial display
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Add initial messages
        message_buffer.add_message("System", f"Selected ticker: {selections['ticker']}")
        message_buffer.add_message(
            "System", f"Analysis date: {selections['analysis_date']}"
        )
        message_buffer.add_message(
            "System",
            f"Selected analysts: {', '.join(analyst.value for analyst in selections['analysts'])}",
        )
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Update agent status to in_progress for the first analyst
        first_analyst = get_initial_analyst_node(analyst_execution_plan)
        message_buffer.update_agent_status(first_analyst, "in_progress")
        analyst_wall_time_tracker.mark_started(selected_analyst_keys[0])
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Create spinner text
        spinner_text = (
            f"Analyzing {selections['ticker']} on {selections['analysis_date']}..."
        )
        update_display(layout, spinner_text, stats_handler=stats_handler, start_time=start_time)

        # Initialize state and get graph args with callbacks
        init_agent_state = graph.propagator.create_initial_state(
            selections["ticker"],
            selections["analysis_date"],
        )
        # Pass callbacks to graph config for tool execution tracking
        # (LLM tracking is handled separately via LLM constructor)
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])

        # Stream the analysis
        trace = []
        for chunk in graph.graph.stream(init_agent_state, **args):
            # Process all messages in chunk, deduplicating by message ID
            for message in chunk.get("messages", []):
                msg_id = getattr(message, "id", None)
                if msg_id is not None:
                    if msg_id in message_buffer._processed_message_ids:
                        continue
                    message_buffer._processed_message_ids.add(msg_id)

                msg_type, content = classify_message_type(message)
                if content and content.strip():
                    message_buffer.add_message(msg_type, content)

                if hasattr(message, "tool_calls") and message.tool_calls:
                    for tool_call in message.tool_calls:
                        if isinstance(tool_call, dict):
                            message_buffer.add_tool_call(tool_call["name"], tool_call["args"])
                        else:
                            message_buffer.add_tool_call(tool_call.name, tool_call.args)

            # Update analyst statuses based on report state (runs on every chunk)
            update_analyst_statuses(
                message_buffer,
                chunk,
                wall_time_tracker=analyst_wall_time_tracker,
            )

            # Research Team - Handle Investment Debate State
            if chunk.get("investment_debate_state"):
                debate_state = chunk["investment_debate_state"]
                bull_hist = debate_state.get("bull_history", "").strip()
                bear_hist = debate_state.get("bear_history", "").strip()
                judge = debate_state.get("judge_decision", "").strip()

                # Only update status when there's actual content
                if bull_hist or bear_hist:
                    update_research_team_status("in_progress")
                if bull_hist:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Bull Researcher Analysis\n{bull_hist}"
                    )
                if bear_hist:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Bear Researcher Analysis\n{bear_hist}"
                    )
                if judge:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Research Manager Decision\n{judge}"
                    )
                    update_research_team_status("completed")
                    message_buffer.update_agent_status("Trader", "in_progress")

            # Trading Team
            if chunk.get("trader_investment_plan"):
                message_buffer.update_report_section(
                    "trader_investment_plan", chunk["trader_investment_plan"]
                )
                if message_buffer.agent_status.get("Trader") != "completed":
                    message_buffer.update_agent_status("Trader", "completed")
                    message_buffer.update_agent_status("Aggressive Analyst", "in_progress")

            # Risk Management Team - Handle Risk Debate State
            if chunk.get("risk_debate_state"):
                risk_state = chunk["risk_debate_state"]
                agg_hist = risk_state.get("aggressive_history", "").strip()
                con_hist = risk_state.get("conservative_history", "").strip()
                neu_hist = risk_state.get("neutral_history", "").strip()
                judge = risk_state.get("judge_decision", "").strip()

                if agg_hist:
                    if message_buffer.agent_status.get("Aggressive Analyst") != "completed":
                        message_buffer.update_agent_status("Aggressive Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Aggressive Analyst Analysis\n{agg_hist}"
                    )
                if con_hist:
                    if message_buffer.agent_status.get("Conservative Analyst") != "completed":
                        message_buffer.update_agent_status("Conservative Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Conservative Analyst Analysis\n{con_hist}"
                    )
                if neu_hist:
                    if message_buffer.agent_status.get("Neutral Analyst") != "completed":
                        message_buffer.update_agent_status("Neutral Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Neutral Analyst Analysis\n{neu_hist}"
                    )
                if judge:
                    if message_buffer.agent_status.get("Portfolio Manager") != "completed":
                        message_buffer.update_agent_status("Portfolio Manager", "in_progress")
                        message_buffer.update_report_section(
                            "final_trade_decision", f"### Portfolio Manager Decision\n{judge}"
                        )
                        message_buffer.update_agent_status("Aggressive Analyst", "completed")
                        message_buffer.update_agent_status("Conservative Analyst", "completed")
                        message_buffer.update_agent_status("Neutral Analyst", "completed")
                        message_buffer.update_agent_status("Portfolio Manager", "completed")

            # Update the display
            update_display(layout, stats_handler=stats_handler, start_time=start_time)

            trace.append(chunk)

        # Streamed chunks are per-node deltas, not full state. Merge them
        # so every report field populated across the run is present.
        final_state = {}
        for chunk in trace:
            final_state.update(chunk)
        decision = graph.process_signal(final_state["final_trade_decision"])

        # Update all agent statuses to completed
        for agent in message_buffer.agent_status:
            message_buffer.update_agent_status(agent, "completed")

        message_buffer.add_message(
            "System", f"Completed analysis for {selections['analysis_date']}"
        )
        message_buffer.add_message("System", analyst_wall_time_tracker.format_summary())

        # Update final report sections
        for section in message_buffer.report_sections.keys():
            if section in final_state:
                message_buffer.update_report_section(section, final_state[section])

        update_display(layout, stats_handler=stats_handler, start_time=start_time)

    # Post-analysis prompts (outside Live context for clean interaction)
    console.print("\n[bold cyan]Analysis Complete![/bold cyan]\n")
    console.print(f"[dim]{analyst_wall_time_tracker.format_summary()}[/dim]")

    # Prompt to save report
    save_choice = typer.prompt("Save report?", default="Y").strip().upper()
    if save_choice in ("Y", "YES", ""):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = Path.cwd() / "reports" / f"{selections['ticker']}_{timestamp}"
        save_path_str = typer.prompt(
            "Save path (press Enter for default)",
            default=str(default_path)
        ).strip()
        save_path = Path(save_path_str)
        try:
            report_file = save_report_to_disk(final_state, selections["ticker"], save_path)
            console.print(f"\n[green]✓ Report saved to:[/green] {save_path.resolve()}")
            console.print(f"  [dim]Complete report:[/dim] {report_file.name}")
        except Exception as e:
            console.print(f"[red]Error saving report: {e}[/red]")

    # Prompt to display full report
    display_choice = typer.prompt("\nDisplay full report on screen?", default="Y").strip().upper()
    if display_choice in ("Y", "YES", ""):
        display_complete_report(final_state)


@app.command()
def analyze(
    checkpoint: bool = typer.Option(
        False,
        "--checkpoint",
        help="Enable checkpoint/resume: save state after each node so a crashed run can resume.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
):
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    run_analysis(checkpoint=checkpoint)


# ============================================================================
# JSON Analysis Mode + Trade Review Commands (OpenClaw integration)
# ============================================================================

import sys
from cli.json_output import build_analysis_result, output_json, serialize_decision
from tradingagents.futures.trade_review import analyze_trade_review


def _build_decision_notify_card(
    final_state: dict, ticker: str, executor_mode: str,
) -> Optional[str]:
    """Build the Discord decision card from the structured PM decision.

    Returns None when the run produced no structured ``FuturesDecision`` —
    the 5-tier rating string alone would make a misleading card. The risk
    gate outcome is read from state (``execution_intent`` /
    ``risk_gate_rejection_reason``) so the card reports what actually
    happened rather than assuming "pass".
    """
    structured = final_state.get("final_decision_structured")
    if structured is None:
        return None
    if isinstance(structured, dict):
        from tradingagents.agents.schemas import FuturesDecision
        structured = FuturesDecision.model_validate(structured)

    from datetime import datetime, timezone
    from tradingagents.notify.discord import format_decision_card

    side = getattr(structured.side, "value", structured.side)
    approved = final_state.get("execution_intent") is not None
    return format_decision_card(
        symbol=ticker,
        direction=str(side),
        leverage=structured.leverage,
        position_size_pct=structured.position_size_pct,
        entry_price=structured.entry_price,      # None → 市价
        stop_loss=structured.stop_loss,          # None → — (absent ≠ 0)
        take_profit=structured.take_profit,
        cycle=structured.time_horizon or "未指定",
        summary=structured.executive_summary or "(无摘要)",
        thesis=structured.investment_thesis,
        risk_gate_status="pass" if approved else "reject",
        risk_gate_reason=final_state.get("risk_gate_rejection_reason"),
        executor_mode=executor_mode,
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )


def _push_decision_card(final_state: dict, ticker: str, executor_mode: str) -> bool:
    """Send the --notify decision card. Fail-open, but never silent.

    In the manual-trading loop the card *is* the order ticket, so every
    path that ends without a delivered card must leave a log trail —
    a silently missing card reads as "no decision today". Returns True
    only when send_discord reported success.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.notify.discord import send_discord

        webhook_url = DEFAULT_CONFIG.get("discord_webhook_url")
        if not webhook_url:
            logger.warning(
                "--notify set but discord_webhook_url is unconfigured "
                "(TRADINGAGENTS_DISCORD_WEBHOOK_URL) — decision card skipped"
            )
            return False
        card = _build_decision_notify_card(final_state, ticker, executor_mode)
        if card is None:
            logger.warning(
                "--notify: run produced no structured decision — decision card skipped"
            )
            return False
        ok = send_discord(card, webhook_url=webhook_url)
        if not ok:
            logger.warning("decision card push failed — see send_discord logs above")
        return ok
    except Exception:
        logger.warning(
            "decision-card push failed — JSON output unaffected", exc_info=True,
        )
        return False


def run_analysis_json(checkpoint: bool = False, notify: bool = False) -> int:
    """Non-interactive JSON analysis mode for OpenClaw integration.
    
    Runs full analysis with automated selections and outputs JSON to stdout.
    Logs go to stderr. Execution mode is forced to dryrun.

    Returns:
        0 on success, 1 on error, 3 when --notify was requested but no
        decision card was delivered (send failure, unconfigured webhook,
        or no structured decision to card). In the scheduled path the
        Discord card is the only deliverable the operator sees, so an
        undelivered card must be distinguishable from success by the
        scheduler — stdout JSON is identical in both cases (OI-N1-1).
    """
    import json
    import os
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.futures.risk_state import default_state_path, load_events, derive_state
    from datetime import datetime, timezone

    try:
        # Auto-select minimal config for JSON mode
        ticker = os.getenv("TRADINGAGENTS_JSON_TICKER", "BTC-USD")

        # Minimal analyst selection (market only for speed)
        selected_analysts = ["market"]
        
        # Create config with dryrun enforced
        config = DEFAULT_CONFIG.copy()
        config["max_debate_rounds"] = 1
        # Cheap default for JSON mode; the documented TRADINGAGENTS_*_THINK_LLM
        # overrides win. The default must be a servable model id: a bare
        # "gemini" 404s, and gemini-2.5-flash is retired for new accounts —
        # either one kills every analyze_json run at the first LLM call.
        config["quick_think_llm"] = os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "gemini-3.6-flash")
        config["deep_think_llm"] = os.getenv("TRADINGAGENTS_DEEP_THINK_LLM", "gemini-3.6-flash")
        config["llm_provider"] = os.getenv("TRADINGAGENTS_LLM_PROVIDER", "google").lower()
        config["output_language"] = os.getenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "English")
        config["checkpoint_enabled"] = checkpoint
        # Force dryrun: analysis must never place orders. EXECUTOR_MODE is
        # the highest-precedence switch (resolve_executor_mode), so overwrite
        # it — an inherited EXECUTOR_MODE=testnet in the shell would otherwise
        # have the analyze path submitting real testnet orders.
        os.environ["EXECUTOR_MODE"] = "dryrun"
        config["futures_executor_mode"] = "dryrun"
        
        stats_handler = StatsCallbackHandler()
        
        # Build analysis plan
        analyst_execution_plan = build_analyst_execution_plan(
            selected_analysts,
            concurrency_limit=config["analyst_concurrency_limit"],
        )
        
        # Create graph
        graph = TradingAgentsGraph(
            selected_analysts,
            config=config,
            debug=False,  # Suppress debug output for JSON mode
            callbacks=[stats_handler],
        )
        
        # Initialize state
        analysis_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        init_agent_state = graph.propagator.create_initial_state(
            ticker,
            analysis_date,
        )
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])
        
        # Stream analysis (collect state without display)
        final_state = {}
        for chunk in graph.graph.stream(init_agent_state, **args):
            final_state.update(chunk)
        
        # Process decision
        decision = graph.process_signal(final_state.get("final_trade_decision"))
        
        # Load current risk state for inclusion in output
        risk_state_path = default_state_path()
        risk_events = load_events(risk_state_path)
        risk_snapshot = derive_state(risk_events, now=datetime.now(timezone.utc))
        
        # Build result
        result = build_analysis_result(
            final_state,
            {"ticker": ticker, "analysis_date": analysis_date, "analysts": selected_analysts},
            decision,
        )
        result["risk_state"] = {
            "open_positions": risk_snapshot.open_positions,
            "daily_realised_pnl_usd": risk_snapshot.daily_realised_pnl_usd,
        }
        # The 5-tier "decision" above is a rating string; the actionable
        # ticket fields (side/leverage/stops/size) and the gate outcome live
        # in state — surface both so a lost Discord card still leaves a
        # complete order ticket on stdout.
        result["decision_structured"] = serialize_decision(
            final_state.get("final_decision_structured")
        )
        result["risk_gate"] = {
            "approved": final_state.get("execution_intent") is not None,
            "rejection_reason": final_state.get("risk_gate_rejection_reason"),
        }

        # A run without a structured FuturesDecision has no actionable
        # ticket — the 5-tier "decision" above is then only parse_rating's
        # fallback (default Hold). Reporting that as status=success would
        # dress a degraded run up as a real 观望 decision (the operator
        # reads Hold and stays out of the market on bad information).
        if final_state.get("final_decision_structured") is None:
            result["status"] = "degraded"
            result["error"] = (
                "no structured FuturesDecision in state — the 5-tier "
                "rating is a text-parse fallback, not an actionable ticket"
            )

        output_json(result)

        # Send Discord decision card if requested. A push failure must
        # leave the JSON stdout untouched (OpenClaw reads it), but the
        # exit code must tell the scheduler the card never arrived —
        # exit 0 with no card reads as "no decision today" (OI-N1-1).
        if notify:
            delivered = _push_decision_card(
                final_state, ticker, config["futures_executor_mode"]
            )
            if not delivered:
                return 3

        return 0
        
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        output_json(result)
        return 1



def check_notify_config() -> int:
    """Check Discord notification configuration.
    
    Validates:
    - webhook_url is configured
    - webhook_url has valid format
    - dedup state file is writable
    
    Returns 0 if ready, 1 if missing or invalid configuration.
    """
    import logging
    from pathlib import Path
    from tradingagents.default_config import DEFAULT_CONFIG

    logger = logging.getLogger(__name__)
    
    webhook_url = DEFAULT_CONFIG.get("discord_webhook_url")
    
    # Check webhook URL
    if not webhook_url:
        print("❌ Discord webhook URL not configured")
        print("   Set TRADINGAGENTS_DISCORD_WEBHOOK_URL environment variable")
        return 1
    
    if not webhook_url.startswith("https://discord.com/api/webhooks/"):
        print("❌ Invalid Discord webhook URL format")
        print(f"   Expected: https://discord.com/api/webhooks/...")
        print(f"   Got:      ...{webhook_url[-20:]}")
        return 1
    
    print(f"✅ Webhook URL configured (ending with ...{webhook_url[-4:]})")
    
    # Check dedup file writeability
    dedup_dir = Path.home() / ".tradingagents"
    try:
        dedup_dir.mkdir(parents=True, exist_ok=True)
        dedup_file = dedup_dir / "notify_dedup.json"
        
        # Try to touch the file
        if dedup_file.exists():
            dedup_file.touch()
        else:
            dedup_file.write_text("{}")
        
        print(f"✅ Dedup state file writable ({dedup_file})")
    except Exception as e:
        print(f"❌ Cannot write dedup state file: {e}")
        return 1
    
    # Check TTL setting
    ttl = DEFAULT_CONFIG.get("notify_dedup_ttl_hours", 6)
    if ttl == 0:
        print("⚠️  Deduplication disabled (TTL = 0)")
    else:
        print(f"✅ Deduplication enabled (TTL = {ttl}h)")
    
    print()
    print("✅ Discord notification ready")
    return 0


@app.command("check-notify")
def check_notify():
    """Check if Discord notification is properly configured."""
    sys.exit(check_notify_config())


@app.command("analyze_json")
def analyze_json(
    checkpoint: bool = typer.Option(
        False,
        "--checkpoint",
        help="Enable checkpoint/resume mode.",
    ),
    notify: bool = typer.Option(
        False,
        "--notify",
        help="Send Discord notification if webhook URL is configured (default: False).",
    ),
):
    """Run analysis in JSON mode for machine consumption (OpenClaw integration).
    
    Non-interactive. Outputs JSON to stdout, logs to stderr.
    Execution is forced to dryrun mode (no real trades).
    """
    sys.exit(run_analysis_json(checkpoint=checkpoint, notify=notify))


@app.command("trade_review")
def trade_review(
    symbol: Optional[str] = typer.Option(
        None,
        "--symbol",
        help="Filter by symbol (e.g. BTCUSDT). If omitted, analyze all.",
    ),
    hours: int = typer.Option(
        24,
        "--hours",
        help="Time window in hours to look back.",
    ),
):
    """Review recent trade decisions, positions, and gate rejections.

    Reads memory log and risk_gate_state.jsonl. Outputs JSON summary.
    """
    try:
        summary = analyze_trade_review(
            symbol=symbol,
            time_window_hours=hours,
        )
        output_json(summary.to_dict())
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        output_json(result)
        sys.exit(1)


@app.command("trade_execute")
def trade_execute(
    decision_file: Optional[Path] = typer.Option(
        None,
        "--decision-file",
        help="Path to decision JSON file. If omitted, reads from stdin.",
    ),
    approved: bool = typer.Option(
        False,
        "--approved",
        help="User approves execution. REQUIRED for execution (--rejected takes precedence).",
    ),
    approved_by: Optional[str] = typer.Option(
        None,
        "--approved-by",
        help="Discord username or identifier of the approver. REQUIRED if --approved.",
    ),
    rejected: bool = typer.Option(
        False,
        "--rejected",
        help="User rejects execution. Only writes trade_skipped event.",
    ),
):
    """Execute a trade decision with mandatory human approval.

    Workflow:
    1. Read decision JSON (from --decision-file or stdin).
    2. Check staleness (default 15 min timeout, env TRADINGAGENTS_FUTURES_APPROVAL_TIMEOUT_MIN).
    3. Verify explicit approval: --approved + --approved-by REQUIRED (no defaults).
    4. Refuse a decision that was already executed (replay protection).
    5. Re-evaluate through risk gate with current state (gate may now reject).
    6. Execute via create_executor (dryrun default, env-selected).
    7. Output JSON result to stdout, logs to stderr.

    Rejection path: --rejected --approved-by <who> writes trade_skipped without executing.

    Exit codes: 0=success, 1=rejection/timeout/gate-fail/duplicate, 2=error.
    """
    import json
    import os
    # Module top-level does `import datetime` (the module); bind the class
    # locally or every `datetime.now(...)` below hits the module and crashes.
    from datetime import datetime, timezone
    from pathlib import Path

    from tradingagents.futures.approval import (
        ApprovalMetadata,
        check_staleness,
        decision_fingerprint,
        find_prior_execution,
        parse_decision_json,
        reconstruct_intent_from_decision,
        record_decision_execution,
        write_trade_skipped_event,
    )
    from tradingagents.futures.executor import create_executor
    from tradingagents.futures.risk_state import default_state_path

    try:
        # ===== Input & Validation =====
        # 1. Read decision JSON
        if decision_file:
            decision_json = json.loads(decision_file.read_text(encoding="utf-8"))
        else:
            decision_json = json.load(sys.stdin)

        # Extract decision and timestamp
        decision, decision_ts = parse_decision_json(decision_json)
        # Fail fast on a missing ticker: defaulting to "UNKNOWN" only defers
        # the failure to the exchange, where it surfaces as a baffling
        # invalid-symbol rejection after the approval has been spent.
        symbol = decision_json.get("analysis", {}).get("ticker")
        if not symbol:
            raise ValueError(
                "decision JSON has no analysis.ticker — refusing to execute "
                "a trade with an unknown symbol"
            )

        # 2. Approval metadata
        now = datetime.now(timezone.utc)
        approval_meta = ApprovalMetadata(
            approved=approved and not rejected,
            approved_by=approved_by or "unknown",
            approved_at=now.replace(microsecond=0).isoformat(),
        )

        # ===== Rejection path (explicit user No) =====
        if rejected:
            reason = "human rejected"
            write_trade_skipped_event(
                reason,
                symbol=symbol,
                approval_metadata=approval_meta,
            )
            result = {
                "status": "rejected",
                "symbol": symbol,
                "reason": reason,
                "approved_by": approved_by,
                "timestamp": now.isoformat(),
            }
            output_json(result)
            sys.exit(1)

        # ===== Staleness check =====
        approval_timeout_min = int(
            os.getenv("TRADINGAGENTS_FUTURES_APPROVAL_TIMEOUT_MIN", "15")
        )
        is_stale, stale_reason = check_staleness(
            decision_ts.isoformat(),
            now=now,
            approval_timeout_minutes=approval_timeout_min,
        )
        if is_stale:
            write_trade_skipped_event(
                stale_reason,
                symbol=symbol,
                approval_metadata=approval_meta,
            )
            result = {
                "status": "stale",
                "symbol": symbol,
                "reason": stale_reason,
                "timestamp": now.isoformat(),
            }
            output_json(result)
            sys.exit(1)

        # ===== Explicit approval check =====
        if not approved:
            reason = "missing --approved flag"
            write_trade_skipped_event(
                reason,
                symbol=symbol,
                approval_metadata=approval_meta,
            )
            result = {
                "status": "unapproved",
                "symbol": symbol,
                "reason": reason,
                "timestamp": now.isoformat(),
            }
            output_json(result)
            sys.exit(1)

        if not approved_by:
            reason = "missing --approved-by parameter"
            write_trade_skipped_event(
                reason,
                symbol=symbol,
                approval_metadata=approval_meta,
            )
            result = {
                "status": "unapproved",
                "symbol": symbol,
                "reason": reason,
                "timestamp": now.isoformat(),
            }
            output_json(result)
            sys.exit(1)

        # ===== Replay protection =====
        # One approval authorises one position. Re-running the CLI on an
        # already-executed decision file (an easy mistake in a manual
        # flow) would open a second same-direction position — the gate's
        # max_concurrent_positions is a total count, not a per-symbol
        # re-entry guard. Checked after the approval gates so a rejected
        # or gate-blocked decision can still be retried.
        fingerprint = decision_fingerprint(decision_json)
        prior = find_prior_execution(fingerprint)
        if prior is not None:
            reason = (
                f"duplicate decision — already executed at {prior.get('ts')} "
                f"(fingerprint {fingerprint}); generate a fresh analysis to trade again"
            )
            write_trade_skipped_event(
                reason,
                symbol=symbol,
                approval_metadata=approval_meta,
            )
            output_json({
                "status": "duplicate",
                "symbol": symbol,
                "reason": reason,
                "first_executed_at": prior.get("ts"),
                "timestamp": now.isoformat(),
            })
            sys.exit(1)

        # ===== Gate re-evaluation =====
        # Executor mode comes from config (dryrun default; testnet via
        # TRADINGAGENTS_FUTURES_EXECUTOR_MODE). Unlike analyze_json, this
        # command is the human-approved execution path, so the configured
        # mode must be honoured — forcing dryrun here would make approval
        # a no-op.
        config = DEFAULT_CONFIG.copy()

        equity_usd = float(config.get("futures_starting_equity_usd", 1000.0))

        # Fetch the mark price BEFORE gate re-evaluation: the gate
        # validates market-order stop side against it and rejects
        # fail-closed when it is unavailable (2026-08-08 review gap —
        # with the fetch after the gate, market-order stops on the wrong
        # side of the price were approved). Venue follows the executor
        # mode so the price checked is the one the order will fill at.
        from tradingagents.futures.executor import resolve_executor_mode
        from tradingagents.futures.market_data import fetch_mark_price

        mark_price = fetch_mark_price(symbol, mode=resolve_executor_mode(config))

        intent, gate_reason = reconstruct_intent_from_decision(
            decision,
            symbol=symbol,
            equity_usd=equity_usd,
            config=config,
            now=now,
            reference_price=mark_price,
        )

        if intent is None:
            write_trade_skipped_event(
                gate_reason or "gate re-evaluation rejected",
                symbol=symbol,
                approval_metadata=approval_meta,
            )
            result = {
                "status": "gate_rejected",
                "symbol": symbol,
                "reason": gate_reason,
                "timestamp": now.isoformat(),
            }
            output_json(result)
            sys.exit(1)

        # ===== Execution =====
        executor = create_executor(config)
        if mark_price is None:
            # A LIMIT decision still carries a usable reference price; a
            # market entry cannot reach here (the gate above rejects it
            # fail-closed on a missing mark price) — this branch stays as
            # a belt-and-braces guard.
            if decision.entry_price:
                mark_price = decision.entry_price
            else:
                reason = "mark price unavailable — refusing to size market order"
                write_trade_skipped_event(
                    reason,
                    symbol=symbol,
                    approval_metadata=approval_meta,
                )
                output_json({
                    "status": "error",
                    "symbol": symbol,
                    "reason": reason,
                    "timestamp": now.isoformat(),
                })
                sys.exit(2)
        # Claim the decision before the exchange call, not after: if this
        # process dies mid-order, a re-run must still be refused — the
        # position may be live, and the dangling-intent path is what
        # reconciles it.
        record_decision_execution(
            fingerprint, symbol=symbol, approval_metadata=approval_meta,
        )

        # execute_with_ledger writes the intent write-ahead (order_submitted)
        # before the exchange call and the paired result event after it
        # (position_opened / position_naked / trade_skipped) — same ledger
        # discipline as the graph's executor node.
        from tradingagents.futures.executor import execute_with_ledger

        exec_result = execute_with_ledger(
            executor, intent,
            equity_usd=equity_usd,
            mark_price=mark_price,
            state_path=default_state_path(),
        )

        result = {
            "status": "executed" if exec_result.success else "execution_failed",
            "symbol": exec_result.symbol,
            "side": exec_result.side,
            "quantity": exec_result.quantity,
            "notional_usd": exec_result.notional_usd,
            "margin_required_usd": exec_result.margin_required_usd,
            "mode": exec_result.mode,
            "order_id": exec_result.order_id,
            "avg_fill_price": exec_result.avg_fill_price,
            "placed_at": exec_result.placed_at,
            "error": exec_result.error,
            "approved_by": approved_by,
            "timestamp": now.isoformat(),
        }
        output_json(result)
        sys.exit(0 if exec_result.success else 1)

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        result = {
            "status": "error",
            "error": f"invalid decision JSON: {str(e)}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        output_json(result)
        sys.exit(2)
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        output_json(result)
        sys.exit(2)


# Must stay at the very end of the module: running `python -m cli.main`
# executes top-down, so app() only sees commands registered above this line.
if __name__ == "__main__":
    app()
