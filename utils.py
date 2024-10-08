import logging
from pathlib import Path
import requests
import subprocess
import os
import shlex
import shutil
import sys
import tempfile
from typing import TypeVar
import urllib.request

logger = logging.getLogger(__name__)

class Target:
    def __init__(self, name, rust_target, clang_target, xwin_arch, vm_name, vm_template):
        self.name = name
        self.rust_target = rust_target
        self.clang_target = clang_target
        self.xwin_arch = xwin_arch
        self.vm_name = vm_name
        self.vm_template = vm_template
        self.is_windows = xwin_arch is not None

    @staticmethod
    def all() -> list["Target"]:
        return [WIN7_X86_64_TARGET, WIN7_X86_TARGET]

    @staticmethod
    def from_name(name):
        return next((x for x in Target.all() if x.name == name))


def run_process(args, env=None, **kwargs):
    if env is None:
        env = dict(os.environ)

    new = set(env.items())
    old = set(os.environ.items())
    extra_env = { k[0]: k[1] for k in new - old }

    logger.info(f"Running {shlex.join(args)} with extra_env {extra_env}")
    return subprocess.run(args, **kwargs, env=env)


WIN7_X86_64_TARGET = Target(name='x86_64-win7', rust_target='x86_64-win7-windows-msvc', clang_target='x86_64-pc-windows-msvc', xwin_arch='x86_64', vm_name='win7_x64', vm_template='vm-windows7sp1-x64.test-agent-pwsh')
WIN7_X86_TARGET = Target(name='i686-win7', rust_target='i686-win7-windows-msvc', clang_target='i686-pc-windows-msvc', xwin_arch='x86', vm_name='win7_x86', vm_template='vm-windows7sp1-x86.test-agent-pwsh')

T = TypeVar('T')
def flatten(l: list[list[T]]) -> list[T]:
    return [item for sublist in l for item in sublist]


def setup_toolchain(xwin_dir: Path, targets: list[Target], force: bool = False):
    if not force and all((target.xwin_arch is None or (xwin_dir / f"lld-link-{target.xwin_arch}").exists() for target in targets)):
        return

    targets = [target for target in targets if target.xwin_arch is not None]

    with tempfile.TemporaryDirectory() as tmp_dir_obj:
        dls = {
            'darwin': 'https://github.com/Jake-Shadle/xwin/releases/download/0.3.1/xwin-0.3.1-aarch64-apple-darwin.tar.gz',
            'linux': 'https://github.com/Jake-Shadle/xwin/releases/download/0.3.1/xwin-0.3.1-x86_64-unknown-linux-musl.tar.gz',
        }

        tmp_dir = Path(tmp_dir_obj)

        urllib.request.urlretrieve(dls[sys.platform], tmp_dir / "xwin.tar.gz")
        run_process(["tar", "xf", str(tmp_dir / "xwin.tar.gz"), "--strip-components=1", "-C", str(tmp_dir)], check=True)
        extra_args = ["--disable-symlinks"] if sys.platform == "darwin" else []
        archs = flatten([["--arch", target.xwin_arch] for target in targets])
        run_process([str(tmp_dir / "xwin"), "--accept-license"] + archs + ["splat", "--output", str(xwin_dir)] + extra_args, check=True)

        for target in targets:
            lld_link_path = xwin_dir / f"lld-link-{target.xwin_arch}"
            lld_link_path.write_text(f"""#!/usr/bin/env bash
            set -e
            XWIN="{str(xwin_dir.resolve())}"
            lld-link "$@" /libpath:$XWIN/crt/lib/{target.xwin_arch} /libpath:$XWIN/sdk/lib/um/{target.xwin_arch} /libpath:$XWIN/sdk/lib/ucrt/{target.xwin_arch}
            """)
            lld_link_path.chmod(0o755)

            clang_cl_path = xwin_dir / f"clang-cl-{target.xwin_arch}"
            clang_cl_path.write_text(f"""#!/usr/bin/env bash
            set -e
            XWIN="{str(xwin_dir.resolve())}"
            clang-cl /imsvc "$XWIN/crt/include" /imsvc "$XWIN/sdk/include/ucrt" /imsvc "$XWIN/sdk/include/um" /imsvc "$XWIN/sdk/include/shared" --target="{str(target.clang_target)}" "$@"
            """)
            clang_cl_path.chmod(0o755)


def build_env(rust_repo: Path, xwin_dir: Path, targets: list[Target]):
    env = dict(os.environ)
    for target in targets:
        if target.xwin_arch is None:
            continue
        env['CC_' + target.rust_target.replace('-', '_')] = str(xwin_dir.resolve() / f'clang-cl-{target.xwin_arch}')
        env['AR_' + target.rust_target.replace('-', '_')] = f'llvm-lib'
        env['RUSTFLAGS'] = f'-Clink-args=-Wl,-rpath,{rust_repo.absolute()}/build/.nix-deps/lib'

    if 'CI' in env:
        del env['CI']
    return env


def get_ref(channel, version):
    if channel == 'nightly':
        res = requests.get(f'https://static.rust-lang.org/dist/{version}/channel-rust-{channel}-git-commit-hash.txt')
        res.raise_for_status()
        return res.text.strip()
    elif channel == 'beta' and 'beta' in version:
        res = requests.get(f'https://static.rust-lang.org/dist/channel-rust-{version}-git-commit-hash.txt')
        res.raise_for_status()
        return res.text.strip()
    elif channel == 'stable' or channel == 'dev':
        return version


def update_rust_patches(rust_repo, patches_dir):
    merge_base = run_process(["git", "merge-base", "HEAD", "FETCH_HEAD"], cwd=rust_repo, text=True, capture_output=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        run_process(["git", "format-patch", f"{merge_base}..HEAD", "-o", str(tmp)], cwd=rust_repo, check=True)

        # we don't use iterdir because it returns a generator, and getting its
        # len is hence much more complicated
        if len(os.listdir(tmp)) == 0:
            return

        patches_dir_exists = patches_dir.exists()
        if patches_dir_exists:
            patches_dir.rename(str(patches_dir) + '.old')
        shutil.copytree(tmp, patches_dir)
        if patches_dir_exists:
            shutil.rmtree(str(patches_dir) + '.old')

def setup_rust_repo(rust_repo: Path, xwin_dir: Path, patches_dir: Path, channel="dev", version=None):
    ref = get_ref(channel, version)

    # Clone rust
    rust_repo.mkdir(parents=True, exist_ok=True)
    run_process(["git", "init"], cwd=rust_repo, check=True)
    is_repo_new = run_process(["git", "rev-parse", "--verify", "HEAD"], cwd=rust_repo).returncode != 0
    is_dirty = (not is_repo_new) and (run_process(["git", "diff-index", "--quiet", "HEAD", "--"], cwd=rust_repo).returncode != 0)
    if is_dirty:
        logger.error("Rust Git repository is dirty. We will not update to the latest commit.")
        raise RuntimeError("Dirty Rust repo")
    else:
        run_process(
            ["git", "fetch", "https://github.com/rust-lang/rust.git"]
            + ([] if ref is None else [ref]),
            cwd=rust_repo,
            check=True,
        )

        # First, update the current patches if necessary.
        if not is_repo_new:
            update_rust_patches(rust_repo, patches_dir)

        # Then, update to the latest HEAD.
        run_process(["git", "reset", "--hard", "FETCH_HEAD"], cwd=rust_repo, check=True)

        # Apply the patches without committing to avoid building with a different
        # commit ID, as it makes downstream consumption fail due to the compiler
        # checking that targets are built with exactly the same version as itself.
        if patches_dir.is_dir():
            for patch in sorted(patches_dir.iterdir()):
                run_process(["git", "apply", str(patch.resolve())], cwd=rust_repo, check=True)

    # TODO: Toml editor?
    config_path = rust_repo / 'config.toml'
    config_data = f"""change-id = 115898
[rust]
channel = "{channel}"

[llvm]
download-ci-llvm = true

"""

    for target in Target.all():
        if target.xwin_arch is not None:
            config_data += f"""
[target.{target.rust_target}]
linker = "{str(xwin_dir.resolve() / ('lld-link-' + target.xwin_arch))}"
cc = "{str(xwin_dir.resolve() / ('clang-cl-' + target.xwin_arch))}"
"""

    config_path.write_text(config_data)

    if channel == 'beta':
        # We want to avoid entering [this codepath](https://github.com/rust-lang/rust/blob/be00c5a9b89161b7f45ba80340f709e8e41122f9/src/bootstrap/src/lib.rs#L1447),
        # so we create a version file containing the value we want.
        version_path = rust_repo / 'version'
        version_path.write_text(version + ' ')

    return rust_repo


def clean_rust_repo(rust_repo: Path):
    run_process(["git", "checkout", "."], cwd=rust_repo, check=True)
