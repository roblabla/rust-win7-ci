from alive_progress import alive_bar
import argparse
import coloredlogs
import hashlib
import logging
from pathlib import Path
import os
import shutil
from utils import Target, run_process, setup_toolchain, setup_rust_repo, clean_rust_repo
from dist import dist
import tomlkit
import boto3
import botocore

logger = logging.getLogger('dist')

def file_digest(fileobj, digest):
    digestobj = hashlib.new(digest)
    buf = bytearray(2**18)  # Reusable buffer to reduce allocations.
    view = memoryview(buf)
    while True:
        size = fileobj.readinto(buf)
        if size == 0:
            break  # EOF
        digestobj.update(view[:size])
    return digestobj

def deploy(s3_url: str, s3_bucket: str, mirror_dir: Path, rust_repo: Path, xwin_dir: Path, channel: str, version: str, targets: list[Target], dont_run_dist: bool, dry_run: bool, force: bool):
    mirror_dir.mkdir(exist_ok=True)

    # First, create the mirror for the given version.
    with open(mirror_dir / "mirror.toml", "w") as f:
        panamax_version = version
        if channel == 'nightly':
            panamax_version = channel + '-' + version

        f.write(f"""[mirror]
retries = 5

[rustup]
sync = true
download_xz = true
download_gz = false
download_threads = 16
source = "https://static.rust-lang.org"
keep_latest_stables = 0
keep_latest_betas = 0
keep_latest_nightlies = 0
pinned_rust_versions = [
    "{panamax_version}"
]
download_dev = true
""")

    # Edit the mirror.toml to only mirror the rustup version we care about.
    run_process(["panamax", "sync", str(mirror_dir)], check=True)

    # Build our custom targets
    if not dont_run_dist:
        for target in targets:
            dist(rust_repo, xwin_dir, target)

    # Add the custom targets to our rustup mirror
    custom_dir = mirror_dir / "dist" / "custom"
    custom_dir.mkdir(exist_ok=True)

    rust_dist_version = version
    if channel == 'nightly':
        rust_dist_version = 'nightly'
    elif channel == 'beta':
        rust_dist_version = 'beta'

    manifest_path = mirror_dir / "dist" / f"channel-rust-{version}.toml"
    if channel == "nightly":
        manifest_path = mirror_dir / "dist" / version / "channel-rust-nightly.toml"
    with open(manifest_path, "r") as manifest_file:
        manifest = tomlkit.load(manifest_file)

    for target in targets:
        in_xz_filename = f"rust-std-{rust_dist_version}-{target.rust_target}.tar.xz"

        # First, we calculate its hash, as we'll use it in the filename and
        # channel manifest.
        with open(rust_repo / "build" / "dist" / in_xz_filename, "rb") as f:
            xz_hash = file_digest(f, "sha256").hexdigest()

        out_xz_filename = f"rust-std-{version}-{target.rust_target}-{xz_hash}.tar.xz"

        # Add the target to the channel. First, we copy the dist file in the
        # mirror.
        shutil.copyfile(rust_repo / "build" / "dist" / in_xz_filename, custom_dir / out_xz_filename)

        # Then, we edit the manifest to add a new target to the rust-std
        # component. This tells rustup how to download the file.
        manifest['pkg']['rust-std']['target'][target.rust_target] = tomlkit.item({
            'available': True,
            'xz_url': f'https://static.rust-lang.org/dist/custom/{out_xz_filename}',
            'xz_hash': xz_hash
        })

        # Finally, we add a reference to our new rust-std target in the rust
        # package. There is one rust package for all hosts capable of running
        # rustc, so we need to iterate over all the target of the rust package.
        #
        # When rustup looks for a component/target to add, it looks for it in
        # the rust.target.{host_target}.extensions table, so we need to add it
        # there.
        for t in manifest['pkg']['rust']['target']:
            manifest_target = manifest['pkg']['rust']['target'][t]
            if 'extensions' in manifest_target:
                # Check if we already have the extension. If we do, do nothing
                # (this allows running deploy multiple times)
                for extension in manifest_target['extensions']:
                    if extension['pkg'] == 'rust-std' and extension['target'] == target.rust_target:
                        break
                else:
                    manifest_target['extensions'].append(tomlkit.item({
                        'pkg': 'rust-std',
                        'target': target.rust_target
                    }))

    # Finally, we write the new manifest.
    with open(manifest_path, "w") as manifest_file:
        tomlkit.dump(manifest, manifest_file)

    # Update hash of channel manifest
    with open(manifest_path, "rb") as f:
        manifest_hash = file_digest(f, "sha256").hexdigest()

    with open(str(manifest_path) + ".sha256", "w") as f2:
        f2.write(manifest_hash)
        f2.write(f"  {manifest_path.name}\n")

    # Make a backup of the manifest with the hash. This way, if it gets
    # overwritten, we can always restore the old one. And because of how we name
    # our built assets with their hash in the name, they should never get
    # overwritten.
    with open(str(manifest_path) + "." + manifest_hash, "w") as manifest_file:
        tomlkit.dump(manifest, manifest_file)


    logger.info("Uploading to the rustup s3 bucket")
    client = boto3.client('s3', endpoint_url=s3_url)

    # Count the number of files we want to transfer.
    numFiles = 0
    for root,dirs,files in os.walk(mirror_dir):
        numFiles += len(files)

    # Setup progress bar
    with alive_bar(numFiles) as bar:
        for root,dirs,files in os.walk(mirror_dir):
            root = Path(root)
            for file in files:
                fullpath = root / file
                relative_path = fullpath.relative_to(mirror_dir)
                # Check checksum, and only upload the file if hash is different.
                # Fallback on size if checksum is not available. If checksum is
                # missing, treat the file as invalid. We should always set the
                # sha256 in object metadata on upload.
                try:
                    obj = client.head_object(Bucket=s3_bucket, Key=str(relative_path))
                    obj_sha256 = obj['Metadata'].get('sha256', None)
                except botocore.exceptions.ClientError as err:
                    if err.response['Error']['Code'] == '404':
                        obj_sha256 = None
                    else:
                        raise

                with open(fullpath, "rb") as f:
                    local_sha256 = file_digest(f, "sha256").hexdigest()

                if obj_sha256 != local_sha256:
                    if obj_sha256 is not None and not force:
                        print(f"Would overwrite file   - {fullpath}")
                        print("Aborting upload")
                        return False

                    if dry_run:
                        if obj_sha256 is None:
                            print(f"Would upload new file - {fullpath}")
                        else:
                            print(f"Would modify file     - {fullpath}")
                    else:
                        client.upload_file(str(fullpath), 'rustup', str(relative_path), extra_args={'Metadata': {'sha256': local_sha256}})
                bar()

    return True


def main():
    parser = argparse.ArgumentParser(
                    prog='run-rustc-tests',
                    description='Runs rustc test for out supported targets')
    parser.add_argument('--rebuild-toolchain', action='store_true')
    parser.add_argument('--version', action='store', required=True)
    parser.add_argument('--channel', action='store', choices=['stable', 'beta', 'nightly'])
    parser.add_argument('--target', action='append', choices=[target.name for target in Target.all()])
    parser.add_argument('--no-dist', action='store_true')
    parser.add_argument('--upload', action='store_true', help='Actually run the deployment. If this argument is not set, this will only do a dry-run.')
    parser.add_argument('--force', action='store_true', help='Overwrite files that already exist in the s3 server.')
    parser.add_argument('--s3-url', action='store', help='Url of the S3 server to upload to.')
    parser.add_argument('--s3-bucket', action='store', help='S3 Bucket to upload to.')
    args = parser.parse_args()

    if args.target is None:
        targets = Target.all()
    else:
        targets = [Target.from_name(target) for target in args.target]

    if args.version is None:
        version = get_version(args.channel)
    else:
        version = args.version

    coloredlogs.install(level='INFO', fmt='%(asctime)s %(name)s %(levelname)s %(message)s')
    xwin_dir = Path('xwin')
    rust_repo = Path('rust')
    patches_dir = Path('patches')
    mirror_dir = Path('mirror')
    setup_toolchain(xwin_dir, targets, force=args.rebuild_toolchain)
    setup_rust_repo(rust_repo, xwin_dir, patches_dir, channel=args.channel, version=args.version)
    res = deploy(s3_url=args.s3_url, s3_bucket=args.s3_bucket, mirror_dir=mirror_dir, rust_repo=rust_repo, xwin_dir=xwin_dir, version=args.version, channel=args.channel, targets=targets, dont_run_dist=args.no_dist, dry_run=not args.upload, force=args.force)
    if not args.upload:
        print("To execute the uploads, rerun the same command with the --upload argument")
    clean_rust_repo(rust_repo)

    if res == False:
        sys.exit(1)


if __name__ == '__main__':
    main()
