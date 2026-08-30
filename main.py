"""Bedtime-story constellation: one writer, four specialized supervisors.

Before submitting the assignment, describe here in a few sentences what you would have built next if you spent 2 more hours on this project:

- Calibrate the judges, not just validate them: run the fixture harness across
  many seeds and measure each supervisor's false-positive/false-negative rate,
  then tune thresholds from data instead of picking 7 by feel. --validate is
  the first step of that.
- A deterministic read-aloud pass (sentence length, clause depth, syllable
  stats computed in Python) feeding age_fit, so the cheapest checks don't
  burn an LLM call.
- A user feedback turn: "sillier", "shorter", "more about the cat" gets
  patched into the category brief and re-enters the same revision loop,
  so feedback is judged by the same supervisors as the first draft.
"""

import argparse
import json
import os
import re
import sys

import openai
import yaml

RUBRIC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rubric.yaml")
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path: str = ENV_PATH) -> None:
    """Load KEY=value lines from a .env file (gitignored) into the environment."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env()


def call_model(prompt: str, max_tokens=3000, temperature=0.1) -> str:
    openai.api_key = os.getenv("OPENAI_API_KEY")  # please use your own openai api key here.
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        stream=False,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return resp.choices[0].message["content"]  # type: ignore


# ---------------------------------------------------------------------------
# Rubric rendering. The rubric is the single source of truth: the writer is
# briefed from craft/categories, the supervisors from supervisors/. Nothing
# below invents vocabulary the rubric doesn't contain.
# ---------------------------------------------------------------------------

def load_rubric() -> dict:
    with open(RUBRIC_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def bullets(items) -> str:
    return "\n".join(f"- {item}" for item in items)


def render_arc(rubric: dict) -> str:
    arc = rubric["craft"]["arc"]
    beats = "\n".join(f"- {b['id']}: {b['purpose']}" for b in arc["beats"])
    return f"{arc['description'].strip()}\n{beats}"


def render_category_brief(rubric: dict, category: str) -> str:
    brief = rubric["categories"][category]
    lines = [f"Category: {category}"]
    for key, value in brief.items():
        lines.append(f"- {key}: {str(value).strip()}")
    return "\n".join(lines)


def render_audience(rubric: dict) -> str:
    a = rubric["audience"]
    lo, hi = a["target_length_words"]
    return (
        f"Audience: children ages {a['ages']}. The story will be read aloud "
        f"by an adult. Target length: {lo}-{hi} words."
    )


def writer_prompt(rubric: dict, category: str, request: str) -> str:
    craft = rubric["craft"]
    moral = craft["moral"]
    return f"""You are a children's storyteller. Write one complete bedtime story.

{render_audience(rubric)}

VOICE
{bullets(craft["voice"])}

LANGUAGE
{bullets(craft["language"])}

STORY ARC
{render_arc(rubric)}

MORAL
{moral["description"].strip()}
Enacted means:
{bullets(moral["enacted_means"])}
Forbidden:
{bullets(moral["forbidden"])}

CATEGORY BRIEF (follow this over your instincts; it overrides generic habits)
{render_category_brief(rubric, category)}

THE CHILD'S REQUEST
{request}

Write the story now. Output only the story text, with a short title on the first line."""


def revision_prompt(rubric: dict, category: str, request: str, draft: str, critiques: list) -> str:
    critique_block = "\n\n".join(
        f"[{name}] {text}" for name, text in critiques
    )
    return f"""You are revising a children's bedtime story. Supervisors reviewed the draft
below and each critique names something from the brief you were originally given.
Fix every critique. Keep everything that already works — this is a revision, not a rewrite.

{render_audience(rubric)}

CATEGORY BRIEF
{render_category_brief(rubric, category)}

STORY ARC (beat ids the critiques refer to)
{render_arc(rubric)}

MORAL RULE
{rubric["craft"]["moral"]["description"].strip()}

THE CHILD'S REQUEST
{request}

CURRENT DRAFT
{draft}

SUPERVISOR CRITIQUES
{critique_block}

Output only the revised story text, with the title on the first line."""


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify(rubric: dict, request: str) -> str:
    names = list(rubric["categories"].keys())
    menu = "\n".join(
        f"- {name}: {rubric['categories'][name]['emphasis']}" for name in names
    )
    prompt = f"""Classify this bedtime-story request into exactly one category.

Categories:
{menu}

Request: {request}

Answer with the category name only, nothing else."""
    answer = call_model(prompt, max_tokens=10, temperature=0.0).strip().lower()
    for name in names:
        if name in answer:
            return name
    return "bedtime_calm"  # it's a bedtime-story system; calm is the safe default


# ---------------------------------------------------------------------------
# Supervisors. Each judge sees ONE dimension, the category context (so
# bedtime_calm's inverted stakes reach the judge, not just the writer), and
# the rubric section it shares vocabulary with.
# ---------------------------------------------------------------------------

# Extra rubric context each supervisor needs so its critique uses the same
# vocabulary the writer was briefed with.
def supervisor_context(rubric: dict, name: str) -> str:
    craft = rubric["craft"]
    if name == "age_fit":
        return "LANGUAGE BRIEF THE WRITER RECEIVED\n" + bullets(craft["language"]) + "\n" + bullets(craft["voice"])
    if name == "narrative_craft":
        return "ARC BRIEF THE WRITER RECEIVED (critique beats by these ids)\n" + render_arc(rubric)
    if name == "moral_integrity":
        moral = craft["moral"]
        return (
            "MORAL BRIEF THE WRITER RECEIVED\n"
            + moral["description"].strip()
            + "\nEnacted means:\n" + bullets(moral["enacted_means"])
            + "\nForbidden:\n" + bullets(moral["forbidden"])
        )
    return ""


def extract_json(text: str):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def run_scored_supervisor(rubric: dict, name: str, category: str, story: str) -> dict:
    spec = rubric["supervisors"][name]
    prompt = f"""You are the {name} supervisor in a children's story pipeline. You evaluate
ONE dimension only. Other supervisors handle everything else; do not score
anything outside your dimension.

{render_audience(rubric)}

CATEGORY CONTEXT — the story was requested and written as this category.
Judge against these expectations, including any inverted ones.
{render_category_brief(rubric, category)}

{supervisor_context(rubric, name)}

YOU EVALUATE
{bullets(spec["evaluates"])}

YOUR CRITIQUE MUST
{bullets(spec["critique_must"])}

{("SCORING RULE (overrides your instincts)" + chr(10) + spec["scoring_rule"].strip()) if spec.get("scoring_rule") else ""}

THE STORY
{story}

Score 1-10. The release threshold on your dimension is {spec["threshold"]};
at or above means acceptable. Be strict but score only your own dimension.
Return strict JSON, nothing else:
{{"score": <integer 1-10>, "critique": "<specific, actionable critique; empty string if nothing needs fixing>"}}"""
    for _ in range(2):  # one retry on unparseable output
        parsed = extract_json(call_model(prompt, max_tokens=500, temperature=0.0))
        if parsed is not None and isinstance(parsed.get("score"), (int, float)):
            return {"score": int(parsed["score"]), "critique": str(parsed.get("critique", ""))}
    return {"score": 0, "critique": f"{name}: judge output was unparseable (failing closed)"}


def run_safety_supervisor(rubric: dict, category: str, story: str) -> dict:
    spec = rubric["supervisors"]["content_safety"]
    prompt = f"""You are the content_safety supervisor in a children's story pipeline. You are
a VETO, not a score: you decide only whether this story may be released to a
child, regardless of how well written it is.

{render_audience(rubric)}

CATEGORY CONTEXT
{render_category_brief(rubric, category)}

BLOCK THE STORY IF IT CONTAINS
{bullets(spec["evaluates"])}

HOW TO CHECK
{spec["procedure"].strip()}

Do not block for quality problems — other supervisors handle quality. Block
only for content that is inappropriate for the age band.

THE STORY
{story}

Return strict JSON, nothing else:
{{"verdict": "pass" or "block",
  "grounds": "<short snake_case label for the category of problem, e.g. unsafe_behaviour_modelled, frightening_imagery; empty if pass>",
  "rationale": "<one or two sentences quoting the problem; empty if pass>"}}"""
    for _ in range(2):
        parsed = extract_json(call_model(prompt, max_tokens=300, temperature=0.0))
        if parsed is not None and parsed.get("verdict") in ("pass", "block"):
            return {
                "verdict": parsed["verdict"],
                "grounds": str(parsed.get("grounds", "")),
                "rationale": str(parsed.get("rationale", "")),
            }
    return {"verdict": "block", "grounds": "judge_error",
            "rationale": "safety judge output was unparseable (failing closed)"}


SCORED = ["age_fit", "narrative_craft", "moral_integrity"]


def judge(rubric: dict, category: str, story: str) -> dict:
    report = {"scores": {}, "safety": run_safety_supervisor(rubric, category, story)}
    for name in SCORED:
        report["scores"][name] = run_scored_supervisor(rubric, name, category, story)
    return report


# ---------------------------------------------------------------------------
# The loop. Three exits: thresholds met, diminishing returns, or the safety
# veto firing twice on the same grounds (then the REQUEST is the suspect and
# we escalate instead of burning iterations). On exhaustion we release the
# best safety-clean draft together with its unmet criteria — never silently.
# ---------------------------------------------------------------------------

def unmet_criteria(rubric: dict, report: dict) -> list:
    unmet = []
    for name in SCORED:
        threshold = rubric["supervisors"][name]["threshold"]
        result = report["scores"][name]
        if result["score"] < threshold:
            unmet.append((name, result["score"], threshold, result["critique"]))
    return unmet


def print_report(iteration, report: dict):
    print(f"\n--- iteration {iteration} ---")
    for name in SCORED:
        r = report["scores"][name]
        print(f"  {name:<18} {r['score']}/10" + (f"  · {r['critique']}" if r["critique"] else ""))
    s = report["safety"]
    verdict = "PASS" if s["verdict"] == "pass" else f"BLOCK ({s['grounds']}) — {s['rationale']}"
    print(f"  {'content_safety':<18} {verdict}")


def run_pipeline(request: str):
    """Classify, draft, judge, revise. Returns (story, category) if a story
    was released (cleanly or with caveats), else None."""
    rubric = load_rubric()
    control = rubric["revision"]

    category = classify(rubric, request)
    print(f"[classifier] category: {category}")

    draft = call_model(writer_prompt(rubric, category, request), temperature=0.9)
    best = None            # (total_score, draft, report) among safety-clean drafts
    prev_scores = None
    block_grounds_seen = []

    for iteration in range(1, control["max_iterations"] + 1):
        report = judge(rubric, category, draft)
        print_report(iteration, report)

        safety = report["safety"]
        scores = {name: report["scores"][name]["score"] for name in SCORED}

        if safety["verdict"] == "block":
            if safety["grounds"] in block_grounds_seen:
                # Exit 3: same grounds twice. The request may be the problem.
                print("\n" + "=" * 60)
                print("STOPPED: the safety supervisor blocked twice on the same "
                      f"grounds ({safety['grounds']}).")
                print("This usually means the request itself, not the draft, is the "
                      "problem. No story is released.")
                print(f"Safety rationale: {safety['rationale']}")
                print("Please adjust the request and try again.")
                return None
            block_grounds_seen.append(safety["grounds"])
        else:
            total = sum(scores.values())
            if best is None or total > best[0]:
                best = (total, draft, report)
            if not unmet_criteria(rubric, report):
                # Exit 1: thresholds met and veto clear.
                print("\n" + "=" * 60)
                print("RELEASED (all supervisors satisfied)\n")
                print(draft)
                return draft, category

        if prev_scores is not None and all(
            scores[name] - prev_scores[name] < 1 for name in SCORED
        ):
            # Exit 2: diminishing returns — revising further isn't converging.
            print("\n[loop] improvement < 1 point on every dimension; stopping early.")
            break
        prev_scores = scores

        if iteration < control["max_iterations"]:
            critiques = [
                (name, c) for name, _, _, c in unmet_criteria(rubric, report) if c
            ]
            if safety["verdict"] == "block":
                critiques.insert(0, ("content_safety (MUST FIX)", safety["rationale"]))
            print("[loop] revising against critiques...")
            draft = call_model(
                revision_prompt(rubric, category, request, draft, critiques),
                temperature=0.7,
            )

    # Exhaustion: never silently ship a failure.
    print("\n" + "=" * 60)
    if best is None:
        print("NO RELEASABLE DRAFT: every iteration was blocked by the safety "
              "supervisor. Grounds seen: " + ", ".join(block_grounds_seen))
        print("Please adjust the request and try again.")
        return None
    total, story, report = best
    unmet = unmet_criteria(rubric, report)
    print("RELEASED WITH CAVEATS — best draft did not meet every threshold:")
    for name, score, threshold, critique in unmet:
        print(f"  - {name}: {score}/{threshold}" + (f" — {critique}" if critique else ""))
    print()
    print(story)
    return story, category


def feedback_loop(request: str, story: str, category: str) -> None:
    """The assignment's 'let the user request changes' idea, reusing the same
    machinery: feedback becomes a critique in the revision prompt, and the
    revised draft is judged by the same four supervisors. Safety can still
    veto a change the user asked for."""
    rubric = load_rubric()
    while True:
        try:
            feedback = input("\nAny changes you'd like? (press Enter to keep the story) ").strip()
        except EOFError:
            return
        if not feedback:
            return
        print("[feedback] revising and re-judging...")
        revised = call_model(
            revision_prompt(rubric, category, request, story,
                            [("user_feedback", feedback)]),
            temperature=0.7,
        )
        report = judge(rubric, category, revised)
        print_report("feedback", report)
        if report["safety"]["verdict"] == "block":
            print("\nThat change made the story unsafe to release "
                  f"({report['safety']['rationale']}). Keeping the previous version.")
            continue
        story = revised
        unmet = unmet_criteria(rubric, report)
        print("\n" + "=" * 60)
        if unmet:
            print("REVISED (note: some dimensions dipped below threshold):")
            for name, score, threshold, _ in unmet:
                print(f"  - {name}: {score}/{threshold}")
            print()
        else:
            print("REVISED (all supervisors still satisfied)\n")
        print(story)


# ---------------------------------------------------------------------------
# Judge validation. One deliberately broken fixture per failure mode plus one
# genuinely good story. Each supervisor must trip on its own fixture and stay
# quiet on the others; the good story must pass all four. A judge that flags
# everything is as useless as one that flags nothing.
# ---------------------------------------------------------------------------

FIXTURES = [
    # (file, category, supervisor expected to fail — None means all must pass)
    ("good_story.txt", "bedtime_calm", None),
    ("too_advanced.txt", "adventure", "age_fit"),
    ("broken_arc.txt", "adventure", "narrative_craft"),
    ("stated_moral.txt", "friendship", "moral_integrity"),
    ("unsafe.txt", "adventure", "content_safety"),
]

ALL_DIMS = SCORED + ["content_safety"]


def validate_judges() -> int:
    rubric = load_rubric()
    failures = 0
    header = f"{'fixture':<20}" + "".join(f"{d:<20}" for d in ALL_DIMS)
    print(header)
    print("-" * len(header))
    for filename, category, expect_fail in FIXTURES:
        with open(os.path.join(FIXTURES_DIR, filename), encoding="utf-8") as f:
            story = f.read()
        report = judge(rubric, category, story)
        row = f"{filename.replace('.txt', ''):<20}"
        for dim in ALL_DIMS:
            if dim == "content_safety":
                tripped = report["safety"]["verdict"] == "block"
                shown = "BLOCK" if tripped else "pass"
            else:
                score = report["scores"][dim]["score"]
                tripped = score < rubric["supervisors"][dim]["threshold"]
                shown = f"{score}/10"
            expected_trip = dim == expect_fail
            ok = tripped == expected_trip
            if not ok:
                failures += 1
            mark = "ok" if ok else ("MISSED" if expected_trip else "FALSE+")
            row += f"{shown + ' ' + mark:<20}"
        print(row)
    print("-" * len(header))
    if failures == 0:
        print("All judges trip on their own failure mode and stay quiet on the others.")
    else:
        print(f"{failures} cell(s) wrong. A judge is either missing its failure "
              "mode (MISSED) or flagging someone else's (FALSE+).")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description="Bedtime-story supervisor constellation")
    parser.add_argument("--validate", action="store_true",
                        help="run the judge-validation fixture suite instead of telling a story")
    parser.add_argument("--request", help="story request (skips the interactive prompt)")
    args = parser.parse_args()

    if args.validate:
        sys.exit(validate_judges())

    request = args.request or input("What kind of story do you want to hear? ")
    released = run_pipeline(request)
    if released and not args.request:  # feedback turn only in interactive mode
        story, category = released
        feedback_loop(request, story, category)


if __name__ == "__main__":
    main()
