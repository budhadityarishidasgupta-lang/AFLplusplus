"""Private SB-T01 trainer CLI; genuine local inference is explicit and bounded."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Sequence
import sys

from brain_core.emotional_learning import (
    AnalysisAdapter, Episode, ImportedMessage, PrivacyProcessor,
    TrainingPaths, TrainingRunner, explicit_operator_authorization,
)


ANNOTATION_FIELDS = {
    "what_happened", "trigger", "explicit_communication", "explicit_emotions",
    "inferred_hypotheses", "alternatives", "context", "emotion_behaviour_links",
    "conflicting_needs", "observed_outcomes", "missing_evidence",
    "candidate_learning", "generalization", "uncertainty",
}
NARRATIVE_FIELDS = {
    "id", "schema_version", "source", "title", "observed_sequence",
    "emotion_hypotheses", "annotation_status", "lesson_candidate",
    "transfer_question", "learning_scope", "outcome_status",
}


def _validate_annotation(annotation):
    if not isinstance(annotation, dict) or set(annotation) != ANNOTATION_FIELDS:
        raise ValueError("Legacy provisional annotation has unsupported or missing fields")
    for field_name in ("what_happened", "trigger", "uncertainty"):
        if not isinstance(annotation[field_name], str) or len(annotation[field_name]) > 2000:
            raise ValueError("Legacy provisional annotation prose is invalid")
    for field_name in ANNOTATION_FIELDS - {"what_happened", "trigger", "uncertainty"}:
        values = annotation[field_name]
        if not isinstance(values, list) or len(values) > 40 or any(
                not isinstance(value, str) or len(value) > 2000 for value in values):
            raise ValueError("Legacy provisional annotation arrays are invalid")
    return annotation


def _normalize_source(source):
    """Keep only minimized source provenance; never pass raw identifiers onward."""
    if isinstance(source, str):
        source = {"reference": source}
    if not isinstance(source, dict):
        raise ValueError("Narrative source must be an object")
    safe = {}
    for key in ("source_id", "reference", "type"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            safe[key] = re.sub(r"[^A-Za-z0-9_.:-]", "_", value.strip())[:160]
    return safe


def _load_seed(paths: TrainingPaths, seed_name: str, limit: int,
               selected_ids: Sequence[str] | set[str] | None = None):
    if not 1 <= limit <= 3:
        raise ValueError("Seed loader limit must be between one and three episodes")
    if selected_ids is not None:
        if not selected_ids or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", item) for item in selected_ids):
            raise ValueError("Requested curated IDs are invalid")
        if len({item.casefold() for item in selected_ids}) != len(selected_ids):
            raise ValueError("Duplicate requested curated IDs")
        if len(selected_ids) > limit:
            raise ValueError("Requested curated IDs exceed the episode limit")
    if Path(seed_name).name != seed_name or not seed_name.lower().endswith(".jsonl"):
        raise ValueError("Seed must be a JSONL filename directly in private input")
    seed_path = paths.path("input") / seed_name
    paths._check_chain(seed_path.parent, expected_root=paths.root)
    with paths.open_read(seed_path, allowed_dir=paths.path("input")) as stream:
        raw = stream.read(5_000_001)
    if len(raw) > 5_000_000:
        raise ValueError("Seed file exceeds the 5 MB limit")
    rows = PrivacyProcessor().process_seed_jsonl(raw.decode("utf-8"))
    normalized = []
    ids_seen = set()
    for row in rows:
        narrative = row.get("format") == "narrative_v1" or "observed_sequence" in row
        episode_id = row.get("id") if narrative else row.get("episode_id")
        if not isinstance(episode_id, str):
            raise ValueError("Curated episode identifier is missing")
        if narrative:
            row = {**row, "episode_id": row["id"], "format": "narrative_v1"}
        if not isinstance(episode_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", episode_id):
            raise ValueError("Invalid curated episode identifier")
        if episode_id in ids_seen:
            raise ValueError("Duplicate seed episode identifiers")
        ids_seen.add(episode_id)
        if selected_ids is not None and episode_id not in selected_ids:
            continue
        if len(json.dumps(row, ensure_ascii=False)) > 100_000:
            raise ValueError("Curated episode exceeds the 100 KB content limit")
        if narrative:
            sequence = row["observed_sequence"]
            if not sequence.strip() or len(sequence) > 12_000:
                raise ValueError("Narrative observed_sequence is empty or exceeds its bound")
            source = _normalize_source(row["source"])
            scrubbed = PrivacyProcessor().process(ImportedMessage(
                f"{episode_id}-narrative", episode_id, "narrative_observation", None, sequence)).text
            # Teaching fields are retained for blind-after-baseline reflection, not baseline.
            hypotheses = row["emotion_hypotheses"]
            if len(json.dumps(hypotheses, ensure_ascii=False)) > 8_000:
                raise ValueError("Emotion hypotheses exceed their bound")
            narrative_record = {
                "experience_type": "narrative_observation",
                "title": row["title"],
                "observed_sequence": scrubbed,
                "emotion_hypotheses": hypotheses,
                "lesson_candidate": row["lesson_candidate"],
                "transfer_question": row["transfer_question"],
                "learning_scope": row["learning_scope"],
                "outcome_status": row["outcome_status"],
                "source_context": source,
            }
            if len(json.dumps(hypotheses, ensure_ascii=False)) > 8_000:
                raise ValueError("Emotion hypotheses exceed their bound")
            annotation = {
                "what_happened": "Provisional narrative annotation; not ground truth.",
                "trigger": "Unknown from narrative alone.",
                "explicit_communication": [],
                "explicit_emotions": [],
                "inferred_hypotheses": [json.dumps(hypotheses, ensure_ascii=False)[:2000]],
                "alternatives": ["The narrative may omit relevant context or perspectives."],
                "context": [json.dumps(source, ensure_ascii=False)[:1000]],
                "emotion_behaviour_links": [],
                "conflicting_needs": [],
                "observed_outcomes": [],
                "missing_evidence": ["Underlying interaction, perspectives, and outcome evidence."],
                "candidate_learning": [row["lesson_candidate"][:2000]] if row["lesson_candidate"].strip() else [],
                "generalization": [row["transfer_question"][:2000]] if row["transfer_question"].strip() else [],
                "uncertainty": "Provisional dataset annotation; independently validate against observed evidence.",
            }
            _validate_annotation(annotation)
            source_id = source.get("source_id", episode_id)
            episode = Episode(
                episode_id,
                [{"source_id": source_id, "record_id": episode_id}],
                [], None, None, [], status="curated_narrative",
                narrative_experience=narrative_record,
            )
        else:
            messages = row["messages"]
            if isinstance(messages, str):
                raise ValueError("Legacy messages must be structured; refusing to invent a speaker")
            if not isinstance(messages, list) or not messages or len(messages) > 80:
                raise ValueError("Each legacy seed item must contain one to 80 structured messages")
            clean_messages, refs = [], []
            for i, message in enumerate(messages):
                if (not isinstance(message, dict) or set(message) - {"speaker_id", "timestamp", "text"}
                        or not isinstance(message.get("speaker_id"), str)
                        or not re.fullmatch(r"speaker_[A-Za-z0-9_-]{1,32}", message["speaker_id"])
                        or not isinstance(message.get("text"), str) or len(message["text"]) > 12_000):
                    raise ValueError("Legacy messages require pseudonymous speaker IDs and bounded text")
                timestamp = message.get("timestamp")
                if timestamp is not None and not isinstance(timestamp, str):
                    raise ValueError("Message timestamps must be text or null")
                text = PrivacyProcessor().process(ImportedMessage(
                    f"{episode_id}-m{i + 1}", episode_id, message["speaker_id"], timestamp,
                    message["text"])).text
                clean_messages.append({"speaker_id": message["speaker_id"], "timestamp": timestamp,
                                       "text": text, "reactions": [], "edited": False,
                                       "has_attachment": False, "attachment_missing": False})
                refs.append({"source_id": row.get("source_ref", row.get("source_refs", episode_id)),
                             "message_id": f"{episode_id}-m{i + 1}",
                             "speaker_id": message["speaker_id"]})
            episode = Episode(episode_id, refs, clean_messages, None, None, [], status="curated_seed")
            annotation = _validate_annotation(row["annotation"])
        normalized.append((episode, annotation))
    if selected_ids is not None:
        available = {episode.episode_id for episode, _ in normalized}
        requested_set = set(selected_ids)
        missing = requested_set - available
        if missing:
            raise ValueError("Requested curated IDs are missing from the seed")
        if isinstance(selected_ids, Sequence) and not isinstance(selected_ids, (str, bytes, set, frozenset)):
            ordered_ids = list(selected_ids)
        else:
            ordered_ids = sorted(selected_ids)
        normalized_by_id = {episode.episode_id: (episode, annotation) for episode, annotation in normalized}
        normalized = [normalized_by_id[episode_id] for episode_id in ordered_ids]
    selected = normalized[:limit] if selected_ids is None else normalized
    if len(selected) > limit or not selected:
        raise ValueError("Curated selection is empty or exceeds the episode bound")
    return [episode for episode, _ in selected], {episode.episode_id: annotation for episode, annotation in selected}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sovereign Brain SB-T01 isolated training backend")
    parser.add_argument("--training-root", required=True, help="Existing absolute private SB-T01 directory")
    parser.add_argument("--mode", choices=("dry-run", "synthetic", "local"), default="dry-run")
    parser.add_argument("--batch-id", default="p1-synthetic-check")
    parser.add_argument("--seed-file", help="Curated JSONL filename inside private input")
    parser.add_argument("--max-episodes", type=int, default=3)
    parser.add_argument("--episode-id", action="append", dest="episode_ids",
                        help="Explicit curated episode ID (repeat for a batch; must exist exactly once)")
    parser.add_argument("--experience-id", help="Study exactly one curated experience (single-experience mode)")
    parser.add_argument("--timeout", type=float, default=60, help="Per-inference timeout, 1 to 600 seconds")
    parser.add_argument("--authorize-local-pilot", action="store_true",
                        help="Explicitly authorize up to three curated episodes using loopback qwen3:0.6b")
    args = parser.parse_args(argv)
    try:
        paths = TrainingPaths(args.training_root)
        if args.mode == "local":
            if not args.authorize_local_pilot:
                raise PermissionError("Use --authorize-local-pilot after confirming dataset permission")
            explicit_operator_authorization(args.authorize_local_pilot)
            if not args.seed_file:
                raise ValueError("--seed-file is required for local pilot")
            if not 1 <= args.timeout <= 600 or not 1 <= args.max_episodes <= 3:
                raise ValueError("Local pilot bounds are invalid")
            from brain_core.model_policy import DEFAULT_LOCAL_MODEL, selected_local_model
            from brain_core.model_api import check_local_readiness
            if selected_local_model() != DEFAULT_LOCAL_MODEL:
                raise ValueError("BRAIN_LOCAL_MODEL must be unset or qwen3:0.6b")
            readiness = check_local_readiness()
            if readiness.get("status") != "AVAILABLE":
                raise RuntimeError(f"Local model readiness failed: {readiness.get('reason')}")
            if args.experience_id and args.episode_ids:
                raise ValueError("Use --experience-id or --episode-id, not both")
            selected_ids = ([args.experience_id] if args.experience_id else
                            args.episode_ids or ["SB-EM-001", "SB-EM-004", "SB-EM-010"])
            if args.experience_id:
                args.max_episodes = 1
            if len({item.casefold() for item in selected_ids}) != len(selected_ids):
                raise ValueError("Duplicate requested episode IDs")
            episodes, annotations = _load_seed(
                paths, args.seed_file, args.max_episodes, selected_ids=set(selected_ids))
            with (paths.path("input") / args.seed_file).open(encoding="utf-8") as seed_stream:
                seed_count = sum(1 for line in seed_stream if line.strip())
            print(f"Seed records loaded: {seed_count}")
            print(f"Episodes selected: {len(episodes)} ({', '.join(episode.episode_id for episode in episodes)})")
            adapter = AnalysisAdapter(provider="ollama", endpoint="http://127.0.0.1:11434/v1",
                                      timeout=args.timeout, authorized=args.authorize_local_pilot)
            runner = TrainingRunner(paths, mode="local", adapter=adapter, authorized=True)
            report = runner.run_local(episodes, annotations, batch_id=args.batch_id,
                                      max_episodes=args.max_episodes)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("outcome") in {"COMPLETED", "RESUMED_COMPLETE"} else 1
        runner = TrainingRunner(paths, mode=args.mode)
        report = {"mode": args.mode, "training_root": str(paths.root),
                  "layout": paths.prepare_layout(), "input_read": False,
                  "inference_calls": 0, "external_providers": []}
        if args.mode == "synthetic":
            report["execution"] = runner.run_synthetic([], batch_id=args.batch_id)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, PermissionError, RuntimeError) as exc:
        print(f"SB-T01 setup refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
