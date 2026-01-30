"""
Activity 4 of the video segment clustering workflow:
Matching clusters to existing signals.
"""

import numpy as np
from sklearn.metrics.pairwise import cosine_distances
from temporalio import activity

from posthog.models.team import Team
from posthog.temporal.ai.video_segment_clustering import constants
from posthog.temporal.ai.video_segment_clustering.models import (
    Cluster,
    MatchClustersActivityInputs,
    MatchingResult,
    SignalMatch,
)

from products.tasks.backend.models import Signal


@activity.defn
async def match_clusters_activity(inputs: MatchClustersActivityInputs) -> MatchingResult:
    """Match new clusters to existing Signals.

    Compares cluster centroids to existing Signal centroids using cosine distance.
    Clusters within threshold are matched, others become new Signals.
    """
    team = await Team.objects.aget(id=inputs.team_id)
    existing_signal_centroids = await _fetch_existing_signal_centroids(team)

    if not existing_signal_centroids:
        # No existing signals, all clusters are new
        return MatchingResult(
            new_clusters=inputs.clusters,
            matched_clusters=[],
        )

    new_clusters: list[Cluster] = []
    matched_clusters: list[SignalMatch] = []

    # Convert signal centroids to arrays for efficient comparison
    signal_ids = list(existing_signal_centroids.keys())
    signal_centroids = np.array(list(existing_signal_centroids.values()))

    for cluster in inputs.clusters:
        cluster_centroid = np.array(cluster.centroid).reshape(1, -1)
        # Calculate cosine distances to all signal centroids
        distances = cosine_distances(cluster_centroid, signal_centroids)[0]
        # Find best match
        min_idx = np.argmin(distances)
        min_distance = distances[min_idx]
        if min_distance < constants.TASK_MATCH_THRESHOLD:
            # Found a match based on centroid similarity
            # Note: This is pretty crude, as we're relying purely on the stability of clustering, and aren't
            # comparing the descriptions in a semantic way per se. For a semantic comparison, an LLM could be
            # a robust verifier, but the cost would increase significantly.
            matched_clusters.append(
                SignalMatch(
                    cluster_id=cluster.cluster_id,
                    signal_id=signal_ids[min_idx],
                    distance=float(min_distance),
                )
            )
        else:
            # No match, this is a new cluster
            new_clusters.append(cluster)

    return MatchingResult(
        new_clusters=new_clusters,
        matched_clusters=matched_clusters,
    )


async def _fetch_existing_signal_centroids(team: Team) -> dict[str, list[float]]:
    """Fetch cluster centroids from existing Signals for deduplication.

    Only match against pending signals (those without a task yet).

    Args:
        team: Team object

    Returns:
        Dictionary mapping signal_id -> centroid embedding
    """
    result: dict[str, list[float]] = {}
    async for signal in Signal.objects.filter(
        team=team,
        task__isnull=True,  # Only match pending signals (not yet accepted)
        cluster_centroid__isnull=False,
    ).values("id", "cluster_centroid"):
        centroid = signal["cluster_centroid"]
        assert centroid is not None  # Filtered by cluster_centroid__isnull=False
        result[str(signal["id"])] = centroid
    return result
