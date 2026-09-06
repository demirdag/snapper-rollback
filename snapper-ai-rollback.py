#!/usr/bin/env -S python3
# -*- coding: utf-8 -*-

"""
Script to rollback to snapper snapshots for the root and home subvolumes.

The filesystem layout follows the snapper ArchWiki suggested layout, with
an additional home snapshot configuration.
"""

from datetime import datetime

import argparse
import btrfsutil
import configparser
import logging
import os
import pathlib
import sys


LOG = logging.getLogger()
LOG.setLevel("INFO")
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
ch = logging.StreamHandler()
ch.setFormatter(formatter)
LOG.addHandler(ch)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Rollback root and/or home to snapper snapshots",
    )
    parser.add_argument(
        "--root-snapid",
        metavar="SNAPID",
        type=str,
        help="ID of snapper snapshot to use for root rollback",
    )
    parser.add_argument(
        "--home-snapid",
        metavar="SNAPID",
        type=str,
        help="ID of snapper snapshot to use for home rollback",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't actually do anything, just print the actions out",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default="/etc/snapper-rollback.conf",
        help="configuration file to use (default: /etc/snapper-rollback.conf)",
    )
    args = parser.parse_args()

    if args.root_snapid is None and args.home_snapid is None:
        parser.error("at least one of --root-snapid or --home-snapid is required")

    return args


def read_config(configfile):
    config = configparser.ConfigParser()
    config.read(configfile)
    return config


def ensure_dir(dirpath, dry_run=False):
    if not os.path.isdir(dirpath):
        try:
            if dry_run:
                LOG.info("mkdir -p '{}'".format(dirpath))
            else:
                os.makedirs(dirpath)
        except OSError as e:
            LOG.fatal("error creating dir '{}': {}".format(dirpath, e))
            raise


def mount_subvol_id5(target, source=None, dry_run=False):
    """
    There is no built-in `mount` function in python, so shell out to mount.
    """

    ensure_dir(target, dry_run=dry_run)

    if not os.path.ismount(target):
        shellcmd = "mount -o subvolid=5 {} {}".format(source or "", target)
        if dry_run:
            LOG.info(shellcmd)
            ret = 0
        else:
            ret = os.system(shellcmd)
        if ret != 0:
            raise OSError("unable to mount {}".format(target))


def rollback_root(
    subvol_main, subvol_main_newname, subvol_rollback_src, dev, dry_run=False
):
    """
    Rename the Linux root subvolume, create a snapshot of the selected
    rollback source at the original root location, and make it default.
    """
    try:
        if dry_run:
            LOG.info("mv {} {}".format(subvol_main, subvol_main_newname))
            LOG.info(
                "btrfs subvolume snapshot {} {}".format(
                    subvol_rollback_src, subvol_main
                )
            )
            LOG.info("btrfs subvolume set-default {}".format(subvol_main))
        else:
            os.rename(subvol_main, subvol_main_newname)
            btrfsutil.create_snapshot(subvol_rollback_src, subvol_main)
            btrfsutil.set_default_subvolume(subvol_main)

        LOG.info(
            "{}Root rollback to {} complete. Reboot to finish".format(
                "[DRY-RUN MODE] " if dry_run else "", subvol_rollback_src
            )
        )
    except FileNotFoundError:
        LOG.fatal(
            f"Missing {subvol_main}: Is {dev} mounted with the option subvolid=5?"
        )
        raise
    except btrfsutil.BtrfsUtilError as e:
        LOG.error(f"{e}")
        if not os.path.isdir(subvol_main):
            LOG.info(f"Moving {subvol_main_newname} back to {subvol_main}")
            if dry_run:
                LOG.warning("mv {} {}".format(subvol_main_newname, subvol_main))
            else:
                os.rename(subvol_main_newname, subvol_main)
        raise


def rollback_home(
    subvol_main,
    subvol_main_newname,
    subvol_rollback_src,
    cache_subvol_relpath,
    dev,
    dry_run=False,
):
    """
    Roll back the home subvolume while preserving a nested cache subvolume.

    The cache subvolume is moved out of the old @home before the old @home is
    replaced, then moved into the new @home at the configured relative path.
    """
    cache_subvol_old = subvol_main_newname / cache_subvol_relpath
    cache_subvol_new = subvol_main / cache_subvol_relpath

    old_parent = subvol_main_newname
    new_parent = subvol_main

    try:
        if dry_run:
            LOG.info("mv {} {}".format(subvol_main, subvol_main_newname))
            LOG.info(
                "btrfs subvolume snapshot {} {}".format(
                    subvol_rollback_src, subvol_main
                )
            )
            LOG.info("preserve cache subvolume {}".format(cache_subvol_relpath))
            LOG.info(
                "mv {} {}".format(cache_subvol_old, cache_subvol_new)
            )
        else:
            # First rename @home. This also moves the cache subvolume's path
            # from @home/... to the timestamped old @home/... path.
            os.rename(subvol_main, subvol_main_newname)

            # Create the rollback copy at the original @home location.
            btrfsutil.create_snapshot(subvol_rollback_src, subvol_main)

            # The snapshot does not include the nested cache subvolume.
            # It can leave an ordinary directory at the cache path, so remove
            # that empty directory before moving the real cache subvolume in.
            if os.path.isdir(cache_subvol_new):
                os.rmdir(cache_subvol_new)

            # Move the existing cache subvolume into the newly created @home.
            os.rename(cache_subvol_old, cache_subvol_new)

        LOG.info(
            "{}Home rollback to {} complete. Reboot to finish".format(
                "[DRY-RUN MODE] " if dry_run else "", subvol_rollback_src
            )
        )
    except FileNotFoundError:
        LOG.fatal(
            f"Missing {subvol_main}: Is {dev} mounted with the option subvolid=5?"
        )
        raise
    except OSError as e:
        LOG.error(f"{e}")

        # If the new @home was not successfully created, restore the old name.
        if not os.path.isdir(subvol_main) and os.path.isdir(subvol_main_newname):
            LOG.info(f"Moving {subvol_main_newname} back to {subvol_main}")
            if dry_run:
                LOG.warning("mv {} {}".format(subvol_main_newname, subvol_main))
            else:
                os.rename(subvol_main_newname, subvol_main)
        raise
    except btrfsutil.BtrfsUtilError as e:
        LOG.error(f"{e}")

        # If snapshot creation failed, restore the old @home name.
        if not os.path.isdir(subvol_main) and os.path.isdir(subvol_main_newname):
            LOG.info(f"Moving {subvol_main_newname} back to {subvol_main}")
            if dry_run:
                LOG.warning("mv {} {}".format(subvol_main_newname, subvol_main))
            else:
                os.rename(subvol_main_newname, subvol_main)
        raise


def get_section_values(config, section):
    mountpoint = pathlib.Path(config.get(section, "mountpoint"))
    subvol_main = mountpoint / config.get(section, "subvol_main")
    subvol_snapshots = config.get(section, "subvol_snapshots")

    try:
        dev = config.get(section, "dev")
    except configparser.NoOptionError:
        dev = None

    return mountpoint, subvol_main, subvol_snapshots, dev


def main():
    args = parse_args()
    config = read_config(args.config)

    operations = []

    if args.root_snapid is not None:
        mountpoint, subvol_main, subvol_snapshots, dev = get_section_values(
            config, "root"
        )
        subvol_rollback_src = (
            mountpoint / subvol_snapshots / args.root_snapid / "snapshot"
        )
        operations.append(
            (
                "root",
                mountpoint,
                subvol_main,
                subvol_rollback_src,
                dev,
            )
        )

    if args.home_snapid is not None:
        mountpoint, subvol_main, subvol_snapshots, dev = get_section_values(
            config, "home"
        )
        cache_subvol_relpath = pathlib.Path(
            config.get("home", "cache_subvol_relpath")
        )
        subvol_rollback_src = (
            mountpoint / subvol_snapshots / args.home_snapid / "snapshot"
        )
        operations.append(
            (
                "home",
                mountpoint,
                subvol_main,
                subvol_rollback_src,
                dev,
                cache_subvol_relpath,
            )
        )

    confirm_typed_value = "CONFIRM"
    try:
        confirmation = input(
            f"Are you SURE you want to rollback? Type '{confirm_typed_value}' to continue: "
        )
        if confirmation != confirm_typed_value:
            LOG.fatal("Bad confirmation, exiting...")
            sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(1)

    date = datetime.now().strftime("%Y-%m-%dT%H:%M")

    try:
        mounted_mountpoints = set()

        for operation in operations:
            kind = operation[0]
            mountpoint = operation[1]

            if str(mountpoint) not in mounted_mountpoints:
                mount_subvol_id5(
                    mountpoint,
                    source=operation[4],
                    dry_run=args.dry_run,
                )
                mounted_mountpoints.add(str(mountpoint))

            subvol_main = operation[2]
            subvol_rollback_src = operation[3]
            dev = operation[4]
            subvol_main_newname = pathlib.Path(f"{subvol_main}{date}")

            if kind == "root":
                rollback_root(
                    subvol_main,
                    subvol_main_newname,
                    subvol_rollback_src,
                    dev,
                    dry_run=args.dry_run,
                )
            else:
                rollback_home(
                    subvol_main,
                    subvol_main_newname,
                    subvol_rollback_src,
                    operation[5],
                    dev,
                    dry_run=args.dry_run,
                )

    except PermissionError as e:
        LOG.fatal("Permission denied: {}".format(e))
        sys.exit(1)


if __name__ == "__main__":
    main()

