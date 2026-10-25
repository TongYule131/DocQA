from pathlib import Path
import json
from app.rag_presentation import context_covered


def test_browser_and_server_share_conservative_context_coverage_cases():
    cases=json.loads((Path(__file__).parent/'fixtures/presentation_context.json').read_text('utf-8'))
    for case in cases:assert context_covered(case['context'],case['text']) is case['covered'],case
