#!/usr/bin/env bash

set -uo pipefail

runtime_error() {
    printf '%s\n' 'RUNTIME_ERROR'
    printf '%s\n' "$1" >&2
    exit 3
}

if (( $# != 3 )); then
    runtime_error 'usage: wait_ready.sh CONTAINER TIMEOUT health|none'
fi

container=$1
timeout=$2
mode=$3

[[ $timeout =~ ^[0-9]+$ ]] || runtime_error 'timeout must be a non-negative integer'
case $mode in
    health | none)
        ;;
    *)
        runtime_error "unknown readiness mode: $mode"
        ;;
esac

state_format='{{.State.Status}}{{printf "\037"}}{{.State.ExitCode}}{{printf "\037"}}{{.State.OOMKilled}}{{printf "\037"}}{{.State.Error}}{{printf "\037"}}{{if .State.Health}}{{.State.Health.Status}}{{end}}'
started_at=$SECONDS
deadline=$((SECONDS + timeout))

inspect_state() {
    local raw_state
    if ! raw_state=$(docker inspect --format "$state_format" "$container" 2>&1); then
        runtime_error "docker inspect failed: $raw_state"
    fi
    IFS=$'\037' read -r status exit_code oom_killed state_error health_status \
        <<<"$raw_state"
}

while :; do
    inspect_state

    if [[ $oom_killed == true || -n $state_error ]]; then
        runtime_error "container state error: status=$status exit_code=$exit_code oom=$oom_killed error=$state_error"
    fi

    case $status in
        exited)
            printf '%s\n' 'TERMINAL'
            printf 'status=%s exit_code=%s\n' "$status" "$exit_code" >&2
            exit 0
            ;;
        running)
            ;;
        created | restarting)
            ;;
        *)
            runtime_error "unexpected container status: $status"
            ;;
    esac

    if [[ $status == running && $mode == health ]]; then
        [[ -n $health_status ]] || runtime_error 'configured HEALTHCHECK has no runtime state'
        if [[ $health_status == healthy ]]; then
            printf '%s\n' 'READY_HEALTH'
            printf 'time_to_healthy_seconds=%s\n' "$((SECONDS - started_at))" >&2
            exit 0
        fi
    fi

    if (( SECONDS >= deadline )); then
        if [[ $mode == none ]]; then
            printf '%s\n' 'RUNNING_NO_PROBE'
            exit 0
        fi
        printf '%s\n' 'PROBE_TIMEOUT'
        printf 'mode=%s timeout=%ss\n' "$mode" "$timeout" >&2
        exit 2
    fi
    sleep 1
done
