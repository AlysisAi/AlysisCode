import sys

if __name__ == "__main__":
    if sys.argv[1:2] == ["--alysis-internal-file-worker"]:
        # Fixed tool dispatcher, not an arbitrary Python/module execution flag.
        from alysis_code.host_read_worker import main

        sys.argv = [sys.argv[0], *sys.argv[2:]]
        raise SystemExit(main())
    # Quiet browser requests relaunch this executable, which is not a Python
    # interpreter. Handle them before importing the CLI or loading user state.
    if sys.argv[1:2] == ["--alysis-internal-open-url"]:
        if len(sys.argv) != 3:
            raise SystemExit(2)
        import webbrowser

        try:
            opened = webbrowser.open(sys.argv[2], new=2)
        except Exception:
            opened = False
        raise SystemExit(0 if opened else 1)

    from alysis_code.cli import app

    app()
