"""Synthetic benchmark signals. Ground truth is held outside investigator inputs."""

BASE_CASES = {
    "application_error": (
        "application exception",
        "ERROR UnhandledApplicationError checkout handler raised ValueError; HTTP 500",
    ),
    "downstream_timeout": (
        "downstream timeout",
        "ERROR DownstreamTimeout dependency=payments elapsed_ms=3000",
    ),
    "database_pool": (
        "database pool exhaustion",
        "ERROR PoolAcquisitionTimeout pool_in_use=5 pool_max=5 waiting=12",
    ),
    "configuration_error": (
        "configuration error",
        "ERROR ConfigurationValidationError required setting PAYMENT_URL missing",
    ),
    "high_cpu": ("CPU saturation", "WARN CpuSaturation cpu_utilization=0.99 sustained_seconds=120"),
    "memory_pressure": (
        "memory pressure",
        "ERROR OOMKilled memory_usage_bytes=536870912 memory_limit_bytes=536870912",
    ),
    "crash_loop": (
        "container restart loop",
        "ERROR CrashLoopBackOff restart_count=8 reason=startup_exception",
    ),
    "deployment_regression": (
        "deployment regression candidate",
        "ERROR ReleaseRegression deployment=v2 first_error_after_release=true previous_release_healthy=true",
    ),
    "network_failure": (
        "network failure candidate",
        "ERROR NetworkConnectFailure dependency=payments reason=connection_refused",
    ),
    "ambiguous": (
        None,
        "INFO latency increased; no causal error, deployment or dependency evidence available",
    ),
}


def scenario_names():
    return [base + suffix for base in BASE_CASES for suffix in ("", "_missing", "_conflicting")]


def scenario_signal(name):
    suffix = next((x for x in ("_missing", "_conflicting") if name.endswith(x)), "")
    base = name.removesuffix(suffix) if suffix else name
    if base not in BASE_CASES:
        raise ValueError("Unknown fixture scenario")
    signal = BASE_CASES[base][1]
    if suffix == "_missing":
        signal = "INFO telemetry unavailable; only aggregate latency signal exists"
    elif suffix == "_conflicting":
        signal += "; CONTRADICTING_SIGNAL measurements inconsistent across replicas; cause not established"
    return signal


def expected_outcome(name):
    base = name.removesuffix("_missing").removesuffix("_conflicting")
    if name.endswith(("_missing", "_conflicting")) or base == "ambiguous":
        return None
    return BASE_CASES[base][0]
