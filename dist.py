import argparse
import coloredlogs
import logging
from pathlib import Path

from utils import Target, build_env, run_process, setup_toolchain, setup_rust_repo, clean_rust_repo

logger = logging.getLogger('dist')

def dist(rust_repo: Path, xwin_dir: Path, target: Target):
    # First, run the build for the correct release.
    run_process(["./x.py", "dist", "rust-std", "--target", target.rust_target], cwd=rust_repo, env=build_env(rust_repo, xwin_dir, [target]), check=True)


def main():
    parser = argparse.ArgumentParser(
                    prog='run-rustc-tests',
                    description='Runs rustc test for out supported targets')
    parser.add_argument('--rebuild-toolchain', action='store_true')
    parser.add_argument('--target', action='append', choices=[target.name for target in Target.all()])
    parser.add_argument('--version', action='store', help='Version to build. If channel is dev (the default), should be a git commit. Otherwise, should be a version or nightly date.')
    parser.add_argument('--channel', action='store', help='Channel to build.', choices=['stable', 'beta', 'nightly', 'dev'], default='dev')
    args = parser.parse_args()

    if args.target is None:
        targets = Target.all()
    else:
        targets = [Target.from_name(target) for target in args.target]

    coloredlogs.install(level='INFO', fmt='%(asctime)s %(name)s %(levelname)s %(message)s')
    xwin_dir = Path('xwin')
    rust_repo = Path('rust')
    patches_dir = Path('patches')
    setup_toolchain(xwin_dir, targets, force=args.rebuild_toolchain)
    setup_rust_repo(rust_repo, xwin_dir, patches_dir, channel=args.channel, version=args.version)
    for target in targets:
        dist(rust_repo, xwin_dir, target)
    clean_rust_repo(rust_repo)


if __name__ == '__main__':
    main()
