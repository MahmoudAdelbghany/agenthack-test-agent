from langgraph.graph import StateGraph, END

from .state import TestGeneratorState
from .nodes import (
    fetch_test_cases,
    analyze_and_filter,
    fetch_steps,
    generate_tests,
    save_results,
)


def should_continue(state: TestGeneratorState) -> str:
    if state.get("error") or not state.get("all_test_cases"):
        return "end"
    return "continue"


def has_relevant_cases(state: TestGeneratorState) -> str:
    if not state.get("relevant_test_cases"):
        return "end"
    return "continue"


def build_test_generator_graph():
    graph = StateGraph(TestGeneratorState)

    graph.add_node("fetch_test_cases", fetch_test_cases)
    graph.add_node("analyze_and_filter", analyze_and_filter)
    graph.add_node("fetch_steps", fetch_steps)
    graph.add_node("generate_tests", generate_tests)
    graph.add_node("save_results", save_results)

    graph.set_entry_point("fetch_test_cases")

    graph.add_conditional_edges(
        "fetch_test_cases", should_continue,
        {"continue": "analyze_and_filter", "end": END}
    )
    graph.add_conditional_edges(
        "analyze_and_filter", has_relevant_cases,
        {"continue": "fetch_steps", "end": END}
    )
    graph.add_edge("fetch_steps", "generate_tests")
    graph.add_edge("generate_tests", "save_results")
    graph.add_edge("save_results", END)

    return graph.compile()
