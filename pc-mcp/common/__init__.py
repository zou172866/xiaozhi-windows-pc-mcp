"""Shared host-side utilities for pc-mcp (H-5).

Consolidates atomic file I/O, dotenv/env resolution, task-ID validation & pruning,
Windows .cmd/.bat shim unwrapping (with Node.js support), batch argument sanitization,
and PID create_time-verified process-tree termination across `agy_sidecar_worker.py`
and `l3_backends/`.
"""

from .dotenv import (
    invalidate_dotenv_cache,
    parse_dotenv_text,
    read_dotenv_map,
    read_dotenv_var,
    read_env_key,
    strip_dotenv_value,
)
from .fs import atomic_write_json, atomic_write_text
from .process import (
    CREATE_NO_WINDOW,
    expand_batch_dp0,
    find_node_exe,
    get_process_create_time,
    is_pid_running,
    is_same_process,
    kill_process_tree,
    prepare_subprocess_command,
    run_subprocess_with_tree_kill,
    safe_float,
    safe_int,
    sanitize_arg_for_windows_batch,
    sanitize_batch_text,
    split_command,
    unwrap_windows_cmd_shim,
)
from .tasks import (
    MAX_ACTIVE_TASK_RECORDS,
    TASK_ID_RE,
    WINDOWS_RESERVED_TASK_NAMES,
    prune_active_tasks,
    resolve_task_file,
    task_recency_key,
    validate_task_id,
)

__all__ = [
    "CREATE_NO_WINDOW",
    "MAX_ACTIVE_TASK_RECORDS",
    "TASK_ID_RE",
    "WINDOWS_RESERVED_TASK_NAMES",
    "atomic_write_json",
    "atomic_write_text",
    "expand_batch_dp0",
    "find_node_exe",
    "get_process_create_time",
    "invalidate_dotenv_cache",
    "is_pid_running",
    "is_same_process",
    "kill_process_tree",
    "parse_dotenv_text",
    "prepare_subprocess_command",
    "prune_active_tasks",
    "read_dotenv_map",
    "read_dotenv_var",
    "read_env_key",
    "resolve_task_file",
    "run_subprocess_with_tree_kill",
    "safe_float",
    "safe_int",
    "sanitize_arg_for_windows_batch",
    "sanitize_batch_text",
    "split_command",
    "strip_dotenv_value",
    "task_recency_key",
    "unwrap_windows_cmd_shim",
    "validate_task_id",
]
