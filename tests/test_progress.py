import sys


def test_stream_command_prints_output_and_keeps_it_for_evidence(
    tmp_path,
    capsys,
):
    from scripts.lib.progress import run_streaming

    result = run_streaming(
        [
            sys.executable,
            "-c",
            "print('first line', flush=True); print('second line', flush=True)",
        ],
        cwd=tmp_path,
        env={},
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == "first line\nsecond line\n"
    assert capsys.readouterr().out == "first line\nsecond line\n"
