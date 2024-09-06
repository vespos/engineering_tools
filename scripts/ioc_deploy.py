#!/usr/bin/python3
"""
ioc-deploy is a script for building and deploying ioc tags from github.

It will take one of two different actions: the normal deploy action,
or a write permissions change on an existing deployed release.

The normal deploy action will create a shallow clone of your IOC in the
standard release area at the correct path and "make" it.
If the tag directory already exists, the script will exit.

In the deploy action, after making the IOC, we'll write-protect all files
and all directories.
We'll also write-protect the top-level directory to help indicate completion.

Note that this means you'll need to restore write permissions if you'd like
to rebuild an existing release on a new architecture or remove it entirely.

Example command:

"ioc-deploy -n ioc-foo-bar -r R1.0.0"

This will clone the repository to the default ioc directory and run make using the
currently set EPICS environment variables, then apply write protection.

With default settings, this will clone
from https://github.com/pcdshub/ioc-foo-bar
to /cds/group/pcds/epics/ioc/foo/bar/R1.0.0
then cd and make and chmod as appropriate.

The second action will not do any git or make actions, it will only find the
release directory and change the file and directory permissions.
This can be done with similar commands as above, adding the subparser command,
and it can be done by passing the specific path you'd like to modify
if this is more convenient for you.

Example commands:

"ioc-deploy update-perms rw -n ioc-foo-bar -r R1.0.0"
"ioc-deploy update-perms ro -n ioc-foo-bar -r R1.0.0"
"ioc-deploy update-perms rw -p /cds/group/pcds/epics/ioc/foo/bar/R1.0.0"
"ioc-deploy update-perms ro -p /cds/group/pcds/epics/ioc/foo/bar/R1.0.0"
"""

import argparse
import enum
import logging
import os
import os.path
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Optional, Tuple

EPICS_SITE_TOP_DEFAULT = "/cds/group/pcds/epics"
GITHUB_ORG_DEFAULT = "pcdshub"
CHMOD_SYMLINKS = os.chmod in os.supports_follow_symlinks
PERMS_CMD = "update-perms"

logger = logging.getLogger("ioc-deploy")


if sys.version_info >= (3, 7, 0):
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class CliArgs:
        """
        Argparse argument types for type checking.
        """

        name: str = ""
        release: str = ""
        ioc_dir: str = ""
        github_org: str = ""
        path_override: str = ""
        auto_confirm: bool = False
        dry_run: bool = False
        verbose: bool = False
        version: bool = False
        permissions: str = ""

    @dataclasses.dataclass(frozen=True)
    class DeployInfo:
        """
        Finalized deploy name and release information.
        """

        deploy_dir: str
        pkg_name: Optional[str]
        rel_name: Optional[str]

else:
    from types import SimpleNamespace

    CliArgs = SimpleNamespace
    DeployInfo = SimpleNamespace


def get_parser(subparser: bool = False) -> argparse.ArgumentParser:
    current_default_target = str(
        Path(os.environ.get("EPICS_SITE_TOP", EPICS_SITE_TOP_DEFAULT)) / "ioc"
    )
    current_default_org = os.environ.get("GITHUB_ORG", GITHUB_ORG_DEFAULT)
    main_parser = argparse.ArgumentParser(
        prog="ioc-deploy",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # main_parser unique arguments that should go first
    main_parser.add_argument(
        "--version", action="store_true", help="Show version number and exit."
    )
    subparsers = main_parser.add_subparsers(help="Subcommands (will not deploy):")
    # perms_parser unique arguments that should go first
    perms_parser = subparsers.add_parser(
        PERMS_CMD,
        help=f"Use 'ioc-deploy {PERMS_CMD}' to update the write permissions of a deployment. See 'ioc-deploy {PERMS_CMD} --help' for more information.",
        description="Update the write permissions of a deployment. This will make all the files read-only (ro), or owner and group writable (rw).",
    )
    perms_parser.add_argument(
        "permissions",
        choices=("ro", "rw"),
        type=force_lower,
        help="Select whether to make the deployment permissions read-only (ro) or read-write (rw).",
    )
    # shared arguments
    for parser in main_parser, perms_parser:
        parser.add_argument(
            "--name",
            "-n",
            action="store",
            default="",
            help="The name of the repository to deploy. This is a required argument. If it does not exist on github, we'll also try prepending with 'ioc-common-'.",
        )
        parser.add_argument(
            "--release",
            "-r",
            action="store",
            default="",
            help="The version of the IOC to deploy. This is a required argument.",
        )
        parser.add_argument(
            "--ioc-dir",
            "-i",
            action="store",
            default=current_default_target,
            help=f"The directory to deploy IOCs in. This defaults to $EPICS_SITE_TOP/ioc, or {EPICS_SITE_TOP_DEFAULT}/ioc if the environment variable is not set. With your current environment variables, this defaults to {current_default_target}.",
        )
        parser.add_argument(
            "--path-override",
            "-p",
            action="store",
            help="If provided, ignore all normal path-selection rules in favor of the specific provided path. This will let you deploy IOCs or apply protection rules to arbitrary specific paths.",
        )
        parser.add_argument(
            "--auto-confirm",
            "--confirm",
            "--yes",
            "-y",
            action="store_true",
            help="Skip the confirmation promps, automatically saying yes to each one.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not deploy anything, just print what would have been done.",
        )
        parser.add_argument(
            "--verbose",
            "-v",
            "--debug",
            action="store_true",
            help="Display additional debug information.",
        )
    # main_parser unique arguments that should go last
    main_parser.add_argument(
        "--github_org",
        "--org",
        action="store",
        default=current_default_org,
        help=f"The github org to deploy IOCs from. This defaults to $GITHUB_ORG, or {GITHUB_ORG_DEFAULT} if the environment variable is not set. With your current environment variables, this defaults to {current_default_org}.",
    )
    if subparser:
        return perms_parser
    return main_parser


def is_yes(option: str, error_on_empty: bool = True) -> bool:
    option = option.strip().lower()
    if option:
        option = option[0]
    if option in ("t", "y"):
        return True
    elif option in ("f", "n"):
        return False
    if not option and not error_on_empty:
        return False
    raise ValueError(f"{option} is not a valid argument")


def force_lower(text: str) -> str:
    return str(text).lower()


class ReturnCode(enum.IntEnum):
    SUCCESS = 0
    EXCEPTION = 1
    NO_CONFIRM = 2


def main_deploy(args: CliArgs) -> int:
    """
    All main steps of the deploy script.

    This will be called when args has neither of apply_write_protection and remove_write_protection

    Will either return an int return code or raise.
    """
    deploy_info = get_deploy_info(args)
    deploy_dir = deploy_info.deploy_dir
    pkg_name = deploy_info.pkg_name
    rel_name = deploy_info.rel_name

    if pkg_name is None or rel_name is None:
        logger.error(
            f"Something went wrong at package/tag normalization: package {pkg_name} at version {rel_name}"
        )
        return ReturnCode.EXCEPTION

    logger.info(f"Deploying {args.github_org}/{pkg_name} at {rel_name} to {deploy_dir}")
    if Path(deploy_dir).exists():
        raise RuntimeError(f"Deploy directory {deploy_dir} already exists! Aborting.")
    if not args.auto_confirm:
        user_text = input("Confirm release source and target? yes/true or no/false\n")
        if not is_yes(user_text, error_on_empty=False):
            return ReturnCode.NO_CONFIRM
    logger.info(f"Cloning IOC to {deploy_dir}")
    rval = clone_repo_tag(
        name=pkg_name,
        github_org=args.github_org,
        release=rel_name,
        deploy_dir=deploy_dir,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    if rval != ReturnCode.SUCCESS:
        logger.error(f"Nonzero return value {rval} from git clone")
        return rval

    logger.info(f"Building IOC at {deploy_dir}")
    rval = make_in(deploy_dir=deploy_dir, dry_run=args.dry_run)
    if rval != ReturnCode.SUCCESS:
        logger.error(f"Nonzero return value {rval} from make")
        return rval
    logger.info(f"Applying write protection to {deploy_dir}")
    rval = set_permissions(
        deploy_dir=deploy_dir, allow_write=False, dry_run=args.dry_run
    )
    if rval != ReturnCode.SUCCESS:
        logger.error(f"Nonzero return value {rval} from set_permissions")
        return rval
    logger.info("IOC clone, make, and permission change complete!")
    return ReturnCode.SUCCESS


def main_perms(args: CliArgs) -> int:
    """
    All main steps of the only-apply-permissions script.

    This will be called when args has at least one of apply_write_protection and remove_write_protection

    Will either return an int code or raise.
    """
    if args.permissions not in ("ro", "rw"):
        logger.error(
            f"Entered main_perms with invalid permissions selected {args.permissions}"
        )
        return ReturnCode.EXCEPTION
    allow_write = args.permissions == "rw"

    deploy_dir = get_perms_target(args)

    if allow_write:
        logger.info(f"Allowing writes to {deploy_dir}")
    else:
        logger.info(f"Preventing writes to {deploy_dir}")
    if not args.auto_confirm:
        user_text = input("Confirm target? yes/true or no/false\n")
        if not is_yes(user_text, error_on_empty=False):
            return ReturnCode.NO_CONFIRM
    try:
        rval = set_permissions(
            deploy_dir=deploy_dir, allow_write=allow_write, dry_run=args.dry_run
        )
    except OSError as exc:
        logger.error(f"OSError during chmod: {exc}")
        error_path = Path(exc.filename)
        logger.error(
            f"Please contact file owner {error_path.owner()} or someone with sudo permissions if you'd like to change the permissions here."
        )
        if allow_write:
            suggest = "ug+w"
        else:
            suggest = "a-w"
        logger.error(
            f"For example, you might try 'sudo chmod -R {suggest} {deploy_dir}' from a server you have sudo access on."
        )
        return ReturnCode.EXCEPTION

    if rval == ReturnCode.SUCCESS:
        logger.info("Write protection change complete!")
    return rval


def get_deploy_info(args: CliArgs) -> DeployInfo:
    """
    Normalize user inputs and figure out where to deploy to.

    Returns the following in a dataclass:
    - The deploy dir (deploy_dir)
    - A normalized package name if applicable, or None (pkg_name)
    - A normalized tag name if applicable, or None (rel_name)
    """
    if args.name and args.github_org:
        pkg_name = finalize_name(
            name=args.name,
            github_org=args.github_org,
            ioc_dir=args.ioc_dir,
            verbose=args.verbose,
        )
    else:
        pkg_name = None
    if pkg_name and args.github_org and args.release:
        rel_name = finalize_tag(
            name=pkg_name,
            github_org=args.github_org,
            release=args.release,
            verbose=args.verbose,
        )
    else:
        rel_name = None
    if args.path_override:
        deploy_dir = args.path_override
    else:
        deploy_dir = get_target_dir(
            name=pkg_name, ioc_dir=args.ioc_dir, release=rel_name
        )

    return DeployInfo(deploy_dir=deploy_dir, pkg_name=pkg_name, rel_name=rel_name)


def get_perms_target(args: CliArgs) -> str:
    """
    Normalize user inputs and figure out which directory to modify.

    Unlike get_deploy_info, this will not check github.
    Instead, we'll check local filepaths for existing targets.

    This is implemented separately to remove the network dependencies,
    but it uses the same helper functions.
    """
    if args.path_override:
        return args.path_override
    _, area, suffix = split_ioc_name(args.name)
    area = find_casing_in_dir(dir=args.ioc_dir, name=area)
    suffix = find_casing_in_dir(dir=str(Path(args.ioc_dir) / area), name=suffix)
    full_name = "-".join(("ioc", area, suffix))
    try_release = release_permutations(args.release)
    for release in try_release:
        target = get_target_dir(name=full_name, ioc_dir=args.ioc_dir, release=release)
        if Path(target).is_dir():
            return target
    raise RuntimeError("Unable to find existing release matching the inputs.")


def find_casing_in_dir(dir: str, name: str) -> str:
    """
    Find a file or directory in dir that matches name aside from casing.

    Raises a RuntimeError if nothing could be found.
    """
    for path in Path(dir).iterdir():
        if path.name.lower() == name.lower():
            return path.name
    raise RuntimeError(f"Did not find {name} in {dir}")


def finalize_name(name: str, github_org: str, ioc_dir: str, verbose: bool) -> str:
    """
    Check if name is present in org, is well-formed, and has correct casing.

    If the name is present, return it, fixing the casing if needed.
    If the name is not present and the correct name can be guessed, guess.
    If the name is not present and cannot be guessed, raise.

    A name is well-formed if it starts with "ioc", is hyphen-delimited,
    and has at least three sections.

    For example, "ioc-common-gigECam" is a well-formed name for the purposes
    of an IOC deployment. "ads-ioc" and "pcdsdevices" are not.

    However, "ads-ioc" will resolve to "ioc-common-ads-ioc".
    Only common IOCs will be automatically discovered using this method.

    Note that GitHub URLs are case-insensitive, so there's no native way to tell
    from a clone step if you've maintained the correct casing information.
    """
    split_name = name.split("-")
    if len(split_name) < 3 or split_name[0] != "ioc":
        new_name = f"ioc-common-{name}"
        logger.warning(f"{name} is not an ioc name, trying {new_name}")
        name = new_name
    logger.debug(f"Checking for {name} in org {github_org}")
    with TemporaryDirectory() as tmpdir:
        try:
            _clone(
                name=name, github_org=github_org, working_dir=tmpdir, verbose=verbose
            )
        except subprocess.CalledProcessError as exc:
            raise ValueError(
                f"Error cloning repo, make sure {name} exists in {github_org} and check your permissions!"
            ) from exc
        readme_text = ""
        # Search for readme in any casing with any file extension
        pattern = "".join(f"[{char.lower()}{char.upper()}]" for char in "readme") + "*"
        for readme_path in (Path(tmpdir) / name).glob(pattern):
            with open(readme_path, "r") as fd:
                readme_text += fd.read()
            logger.debug("Successfully read repo readme for backup casing check")
        if not readme_text:
            logger.debug("Unable to read repo readme for backup casing check")
    logger.debug("Checking deploy area for casing")
    # GitHub URLs are case-insensitive, so we need further checks
    # REST API is most reliable but requires different auth
    # Checking existing directories is ideal because it ensures consistency with earlier releases
    # Check the readme last as a backup
    repo_dir = Path(
        get_target_dir(name=name, ioc_dir=ioc_dir, release="placeholder")
    ).parent
    if repo_dir.exists():
        logger.debug(f"{repo_dir} exists, using as-is")
        return name
    logger.info(f"{repo_dir} does not exist, checking for other casings")
    _, area, suffix = split_ioc_name(name)
    # First, check for casing on area
    try:
        area = find_casing_in_dir(dir=ioc_dir, name=area)
    except RuntimeError:
        logger.info("This is a new area, checking readme for casing")
        name = casing_from_readme(name=name, readme_text=readme_text)
        logger.info(f"Using casing: {name}")
        return name
    logger.info(f"Using {area} as the area")

    try:
        suffix = find_casing_in_dir(dir=str(Path(ioc_dir) / area), name=suffix)
    except RuntimeError:
        logger.info("This is a new ioc, checking readme for casing")
        # Use suffix from readme but keep area from directory search
        casing = casing_from_readme(name=name, readme_text=readme_text)
        suffix = split_ioc_name(casing)[2]
    logger.info(f"Using {suffix} as the name")

    name = "-".join(("ioc", area, suffix))
    logger.info(f"Using casing: {name}")
    return name


def split_ioc_name(name: str) -> Tuple[str, str, str]:
    """
    Split an IOC name into ioc, area, suffix
    """
    return tuple(name.split("-", maxsplit=2))


def casing_from_readme(name: str, readme_text: str) -> str:
    """
    Returns the correct casing of name in readme_text if available.

    If this isn't available, check for the suffix in readme_text and use it.
    This helps for IOCs that only have the suffix in the readme.

    If neither is available, return the original name and log some warnings.
    """
    try:
        return casing_from_text(uncased=name, casing_source=readme_text)
    except ValueError:
        ...
    _, area, suffix = split_ioc_name(name)
    try:
        new_suffix = casing_from_text(uncased=suffix, casing_source=readme_text)
    except ValueError:
        logger.warning(
            "Did not find any casing information in readme. Please double-check the name!"
        )
        return name
    logger.warning(
        "Did not find area casing information in readme. Please double-check the name!"
    )
    return f"ioc-{area}-{new_suffix}"


def casing_from_text(uncased: str, casing_source: str) -> str:
    """
    Returns the casing of the uncased text as found in casing_source.

    Raises ValueError if this fails.
    """
    index = casing_source.lower().index(uncased.lower())
    return casing_source[index : index + len(uncased)]


def finalize_tag(name: str, github_org: str, release: str, verbose: bool) -> str:
    """
    Check if release is present in the org.

    We'll try a few prefix options in case the user has a typo.
    In order of priority with no repeats:
    - user's input
    - R1.0.0
    - v1.0.0
    - 1.0.0
    """
    try_release = release_permutations(release=release)

    with TemporaryDirectory() as tmpdir:
        for rel in try_release:
            logger.debug(f"Checking for release {rel} in {github_org}/{name}")
            try:
                _clone(
                    name=name,
                    github_org=github_org,
                    release=rel,
                    working_dir=tmpdir,
                    target_dir=rel,
                    verbose=verbose,
                )
            except subprocess.CalledProcessError:
                logger.warning(f"Did not find release {rel} in {github_org}/{name}")
            else:
                logger.info(f"Release {rel} exists in {github_org}/{name}")
                return rel
    raise ValueError(f"Unable to find {release} in {github_org}/{name}")


def release_permutations(release: str) -> List[str]:
    """
    Given a user-provided tag/release name, return all the variants we'd like to check for.
    """
    try_release = [release]
    if release.startswith("R"):
        try_release.extend([f"v{release[1:]}", f"{release[1:]}"])
    elif release.startswith("v"):
        try_release.extend([f"R{release[1:]}", f"{release[1:]}"])
    elif release[0].isalpha():
        try_release.extend([f"R{release[1:]}", f"v{release[1:]}", f"{release[1:]}"])
    else:
        try_release.extend([f"R{release}", f"v{release}"])
    return try_release


def get_target_dir(name: str, ioc_dir: str, release: str) -> str:
    """
    Return the directory we'll deploy the IOC in.

    This will look something like:
    /cds/group/pcds/epics/ioc/common/gigECam/R1.0.0
    """
    split_name = name.split("-")
    return str(Path(ioc_dir) / split_name[1] / "-".join(split_name[2:]) / release)


def clone_repo_tag(
    name: str,
    github_org: str,
    release: str,
    deploy_dir: str,
    dry_run: bool,
    verbose: bool,
) -> int:
    """
    Create a shallow clone of the git repository in the correct location.
    """
    # Make sure the parent dir exists
    parent_dir = Path(deploy_dir).resolve().parent
    if dry_run:
        logger.info(f"Dry-run: make {parent_dir} if not existing")
    else:
        logger.debug(f"Ensure {parent_dir} exists")
        parent_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        logger.debug("Dry-run: skip git clone")
        return ReturnCode.SUCCESS
    else:
        return _clone(
            name=name,
            github_org=github_org,
            release=release,
            target_dir=deploy_dir,
            verbose=verbose,
        ).returncode


def make_in(deploy_dir: str, dry_run: bool) -> int:
    """
    Shell out to make in the deploy dir
    """
    if dry_run:
        logger.info(f"Dry-run: skipping make in {deploy_dir}")
        return ReturnCode.SUCCESS
    else:
        return subprocess.run(["make"], cwd=deploy_dir).returncode


def set_permissions(deploy_dir: str, allow_write: bool, dry_run: bool) -> int:
    """
    Apply or remove write permissions from a deploy repo.

    allow_write=True involves adding "w" permissions to all files and directories
    for the owner and for the group. "w" permissions will never be added for other users.
    We will also add write permissions to the top-level directory.

    allow_write=False involves removing the "w" permissions from all files and directories
    for the owner, group, and other users.
    We will also remove write permissions from the top-level direcotry.
    """
    if dry_run and not os.path.isdir(deploy_dir):
        # Dry run has nothing to do if we didn't build the dir
        # Most things past this point will error out
        logger.info("Dry-run: skipping permission changes on never-made directory")
        return ReturnCode.SUCCESS

    set_one_permission(deploy_dir, allow_write=allow_write, dry_run=dry_run)

    for dirpath, dirnames, filenames in os.walk(deploy_dir):
        for name in dirnames + filenames:
            full_path = os.path.join(dirpath, name)
            set_one_permission(full_path, allow_write=allow_write, dry_run=dry_run)

    return ReturnCode.SUCCESS


def set_one_permission(path: str, allow_write: bool, dry_run: bool) -> None:
    """
    Given some file, adjust the permissions as needed for this script.

    If allow_write is True, allow owner and group writes.
    If allow_write is False, prevent all writes.

    During a dry run, log what would be changed at info level without
    making any changes. This log will be present at debug level
    for verbose mode during real changes.
    """
    if os.path.islink(path) and not CHMOD_SYMLINKS:
        logger.debug(f"Skip {path}, os doesn't support follow_symlinks in chmod.")
        return
    mode = os.stat(path, follow_symlinks=False).st_mode
    if allow_write:
        new_mode = mode | stat.S_IWUSR | stat.S_IWGRP
    else:
        new_mode = mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    if dry_run:
        logger.info(f"Dry-run: would change {path} from {oct(mode)} to {oct(new_mode)}")
    else:
        logger.debug(f"Changing {path} from {oct(mode)} to {oct(new_mode)}")
        if CHMOD_SYMLINKS:
            os.chmod(path, new_mode, follow_symlinks=False)
        else:
            os.chmod(path, new_mode)


def get_version() -> str:
    """
    Determine what version of engineering_tools is being used
    """
    # Possibility 1: git clone (yours)
    try:
        return subprocess.check_output(
            ["git", "-C", str(Path(__file__).resolve().parent), "describe", "--tags"],
            universal_newlines=True,
        ).strip()
    except subprocess.CalledProcessError:
        ...
    # Possibility 2: release dir (someone elses)
    ver = str(Path(__file__).resolve().parent.parent.stem)
    if ver.startswith("R"):
        return ver
    else:
        # We tried our best
        return "unknown.dev"


def _clone(
    name: str,
    github_org: str,
    release: str = "",
    working_dir: str = "",
    target_dir: str = "",
    verbose: bool = False,
) -> subprocess.CompletedProcess:
    """
    Clone the repo or raise a subprocess.CalledProcessError
    """
    cmd = ["git", "clone", f"git@github.com:{github_org}/{name}", "--depth", "1"]
    if release:
        cmd.extend(["-b", release])
    if target_dir:
        cmd.append(target_dir)
    kwds = {"check": True}
    if working_dir:
        kwds["cwd"] = working_dir
    if not verbose:
        kwds["stdout"] = subprocess.PIPE
        kwds["stderr"] = subprocess.PIPE
    logger.debug(f"Calling '{' '.join(cmd)}' with kwargs {kwds}")
    return subprocess.run(cmd, **kwds)


def print_help_text_for_readme():
    """
    Prints a text blob for me to paste into the release notes table.
    """
    text = get_parser().format_help() + "\n" + get_parser(subparser=True).format_help()
    lines = text.splitlines()
    output = []
    for line in lines:
        if not line:
            output.append("&nbsp;")
        elif line[0] == " ":
            output.append("&nbsp;" + line[1:])
        else:
            output.append(line)
    print("\n".join(output))


def rearrange_sys_argv_for_subcommands():
    """
    Small trick to help argparse deal with my optional subcommand.

    This will make argv like this:
    ioc-deploy -p some_path perms ro
    be interpretted the same as:
    ioc-deploy perms ro -p some_path

    Otherwise, the first example here is interpretted as if -p was never passed,
    which could be confusing.
    """
    try:
        perms_index = sys.argv.index(PERMS_CMD)
    except ValueError:
        return
    if perms_index == 1:
        return
    subcmd = sys.argv[perms_index]
    try:
        mode = sys.argv[perms_index + 1]
    except IndexError:
        return
    sys.argv.remove(subcmd)
    sys.argv.remove(mode)
    sys.argv.insert(1, subcmd)
    sys.argv.insert(2, mode)


def _main() -> int:
    """
    Thin wrapper of main() for log setup, args handling, and high-level error handling.
    """
    rearrange_sys_argv_for_subcommands()
    args = CliArgs(**vars(get_parser().parse_args()))
    if args.verbose:
        level = logging.DEBUG
        fmt = "%(levelname)-8s %(name)s:%(lineno)d: %(message)s"
    else:
        level = logging.INFO
        fmt = "%(levelname)-8s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt)
    logger.debug(f"args are {args}")
    try:
        if args.version:
            print(get_version())
            return ReturnCode.SUCCESS
        logger.info("ioc-deploy: checking inputs")
        if not (args.name and args.release) and not args.path_override:
            logger.error(
                "Must provide both --name and --release, or --path-override. Check ioc-deploy --help for usage."
            )
            return ReturnCode.EXCEPTION
        try:
            do_perms_cmd = args.permissions
        except AttributeError:
            do_perms_cmd = False
        if do_perms_cmd:
            rval = main_perms(args)
        else:
            rval = main_deploy(args)

    except Exception as exc:
        logger.error(exc)
        logger.debug("Traceback", exc_info=True)
        rval = ReturnCode.EXCEPTION

    if rval == ReturnCode.SUCCESS:
        logger.info("ioc-deploy completed successfully")
    elif rval == ReturnCode.EXCEPTION:
        logger.error("ioc-deploy errored out")
    elif rval == ReturnCode.NO_CONFIRM:
        logger.info("ioc-deploy cancelled")
    return rval


if __name__ == "__main__":
    exit(_main())