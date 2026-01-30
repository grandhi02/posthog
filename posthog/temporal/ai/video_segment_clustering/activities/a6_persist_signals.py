"""
Activity 6 of the video segment clustering workflow:
Persisting Signals and SignalReferences.
"""

from datetime import timedelta

from django.utils import timezone as django_timezone

import structlog
from asgiref.sync import sync_to_async
from temporalio import activity

from posthog.models.team import Team
from posthog.temporal.ai.video_segment_clustering.data import count_distinct_persons
from posthog.temporal.ai.video_segment_clustering.models import PersistSignalsActivityInputs, PersistSignalsResult
from posthog.temporal.ai.video_segment_clustering.priority import (
    calculate_priority_score,
    calculate_task_metrics,
    parse_datetime_as_utc,
    parse_timestamp_to_seconds,
)

from products.tasks.backend.models import Signal, SignalReference

logger = structlog.get_logger(__name__)


@activity.defn
async def persist_signals_activity(inputs: PersistSignalsActivityInputs) -> PersistSignalsResult:
    """Persists new Signals and updates existing relevant ones, creating SignalReferences in the process."""
    team = await Team.objects.aget(id=inputs.team_id)

    segment_lookup = {s.document_id: s for s in inputs.segments}

    signal_ids: list[str] = []
    signals_created = 0
    signals_updated = 0

    # Build cluster_to_signal mapping as we create signals
    cluster_to_signal: dict[int, str] = {}

    # 1. Create new Signals for new clusters
    for cluster in inputs.new_clusters:
        label = inputs.labels.get(cluster.cluster_id)
        if not label:
            logger.warning("No label found for new cluster, skipping", cluster_id=cluster.cluster_id)
            continue

        cluster_segments = [segment_lookup[sid] for sid in cluster.segment_ids if sid in segment_lookup]
        metrics = await calculate_task_metrics(team, cluster_segments)

        priority = calculate_priority_score(
            relevant_user_count=metrics["relevant_user_count"],
        )

        signal = await Signal.objects.acreate(
            team=team,
            title=label.title,
            task_prompt=label.description,
            cluster_centroid=cluster.centroid,
            cluster_centroid_updated_at=django_timezone.now(),
            priority_score=priority,
            relevant_user_count=metrics["relevant_user_count"],
            occurrence_count=metrics["occurrence_count"],
            last_occurrence_at=metrics["last_occurrence_at"],
        )

        signal_ids.append(str(signal.id))
        cluster_to_signal[cluster.cluster_id] = str(signal.id)
        signals_created += 1

        logger.info(
            "Created signal from cluster",
            signal_id=str(signal.id),
            cluster_id=cluster.cluster_id,
            cluster_size=cluster.size,
        )

    # 2. Update existing Signals for matched clusters (idempotent - only count new segments)
    for match in inputs.matched_clusters:
        try:
            signal = await Signal.objects.aget(id=match.signal_id)
        except Signal.DoesNotExist:
            logger.warning("Matched signal not found", signal_id=match.signal_id)
            continue

        cluster_to_signal[match.cluster_id] = match.signal_id

        # Find segments for this matched cluster from segment_to_cluster
        matched_segment_ids = [doc_id for doc_id, cid in inputs.segment_to_cluster.items() if cid == match.cluster_id]
        cluster_segments = [segment_lookup[sid] for sid in matched_segment_ids if sid in segment_lookup]

        if cluster_segments:
            # Check which segments already have SignalReferences for this signal (idempotency)
            relevant_signal_references = [
                signal_reference
                async for signal_reference in SignalReference.objects.filter(signal_id=match.signal_id).only(
                    "session_id", "start_time", "end_time", "distinct_id"
                )
            ]
            existing_refs: set[str] = set()
            for ref in relevant_signal_references:
                end_time_str = ref.end_time.isoformat() if ref.end_time else ""
                existing_refs.add(f"{ref.session_id}:{ref.start_time.isoformat()}:{end_time_str}")

            # Filter to only NEW segments (not already linked to this signal)
            new_segments = []
            for seg in cluster_segments:
                session_start = parse_datetime_as_utc(seg.session_start_time)
                abs_start = session_start + timedelta(seconds=parse_timestamp_to_seconds(seg.start_time))
                abs_end = session_start + timedelta(seconds=parse_timestamp_to_seconds(seg.end_time))
                ref_key = f"{seg.session_id}:{abs_start.isoformat()}:{abs_end.isoformat()}"
                if ref_key not in existing_refs:
                    new_segments.append(seg)

            if new_segments:
                # Get all distinct_ids from existing refs + new segments for accurate user count
                existing_distinct_ids = {ref.distinct_id for ref in relevant_signal_references}
                new_distinct_ids = {seg.distinct_id for seg in new_segments}
                relevant_user_count = await sync_to_async(count_distinct_persons)(
                    team, list(existing_distinct_ids | new_distinct_ids)
                )

                signal.relevant_user_count = relevant_user_count
                signal.occurrence_count = (signal.occurrence_count or 0) + len(new_segments)

                # Find most recent occurrence from new segments
                for segment in new_segments:
                    session_start_time = parse_datetime_as_utc(segment.session_start_time)
                    segment_start_time = session_start_time + timedelta(
                        seconds=parse_timestamp_to_seconds(segment.start_time)
                    )
                    if signal.last_occurrence_at is None or segment_start_time > signal.last_occurrence_at:
                        signal.last_occurrence_at = segment_start_time

                signal.priority_score = calculate_priority_score(
                    relevant_user_count=signal.relevant_user_count,
                )

                await signal.asave()
                signals_updated += 1

                logger.info(
                    "Updated signal from matched cluster",
                    signal_id=str(signal.id),
                    cluster_id=match.cluster_id,
                    new_segments=len(new_segments),
                    skipped_existing=len(cluster_segments) - len(new_segments),
                )

        signal_ids.append(str(signal.id))

    # 3. Create SignalReference records in bulk (idempotent via ignore_conflicts)
    refs_to_create: list[SignalReference] = []

    for segment in inputs.segments:
        cluster_id = inputs.segment_to_cluster.get(segment.document_id)
        if cluster_id is None:
            continue

        signal_id = cluster_to_signal.get(cluster_id)
        if not signal_id:
            continue

        session_start_time = parse_datetime_as_utc(segment.session_start_time)
        segment_start_time = session_start_time + timedelta(seconds=parse_timestamp_to_seconds(segment.start_time))
        segment_end_time = session_start_time + timedelta(seconds=parse_timestamp_to_seconds(segment.end_time))

        refs_to_create.append(
            SignalReference(
                signal_id=signal_id,
                session_id=segment.session_id,
                start_time=segment_start_time,
                end_time=segment_end_time,
                distinct_id=segment.distinct_id,
                content=segment.content,
                distance_to_centroid=None,
            )
        )

    if refs_to_create:
        created_refs = await SignalReference.objects.abulk_create(refs_to_create, ignore_conflicts=True)
        links_created = len(created_refs)
    else:
        links_created = 0

    return PersistSignalsResult(
        signals_created=signals_created,
        signals_updated=signals_updated,
        signal_ids=signal_ids,
        links_created=links_created,
    )
