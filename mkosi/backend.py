# SPDX-License-Identifier: LGPL-2.1+

from __future__ import annotations

import argparse
import ast
import collections
import contextlib
import dataclasses
import enum
import errno
import functools
import importlib
import math
import os
import platform
import pwd
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from types import FrameType
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    Callable,
    Deque,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

from mkosi.distributions import DistributionInstaller
from mkosi.syscall import (
    blkpg_add_partition,
    blkpg_del_partition,
    block_reread_partition_table,
)

T = TypeVar("T")
V = TypeVar("V")
PathString = Union[Path, str]


def shell_join(cmd: Sequence[PathString]) -> str:
    return " ".join(shlex.quote(str(x)) for x in cmd)


@contextlib.contextmanager
def set_umask(mask: int) -> Iterator[int]:
    old = os.umask(mask)
    try:
        yield old
    finally:
        os.umask(old)


def print_between_lines(s: str) -> None:
    size = os.get_terminal_size()
    print('-' * size.columns)
    print(s.rstrip('\n'))
    print('-' * size.columns)


def roundup(x: int, step: int) -> int:
    return ((x + step - 1) // step) * step


# These types are only generic during type checking and not at runtime, leading
# to a TypeError during compilation.
# Let's be as strict as we can with the description for the usage we have.
if TYPE_CHECKING:
    CompletedProcess = subprocess.CompletedProcess[Any]
    Popen = subprocess.Popen[Any]
else:
    CompletedProcess = subprocess.CompletedProcess
    Popen = subprocess.Popen


class MkosiException(Exception):
    """Leads to sys.exit"""


class MkosiNotSupportedException(MkosiException):
    """Leads to sys.exit when an invalid combination of parsed arguments happens"""


# This global should be initialized after parsing arguments
ARG_DEBUG: Set[str] = set()


class Parseable:
    "A mix-in to provide conversions for argparse"

    def __str__(self) -> str:
        """Return the member name without the class name"""
        return cast(str, getattr(self, "name"))

    @classmethod
    def from_string(cls: Any, name: str) -> Any:
        """A convenience method to be used with argparse"""
        try:
            return cls[name]
        except KeyError:
            raise argparse.ArgumentTypeError(f"unknown Format: {name!r}")

    @classmethod
    def parse_list(cls: Any, string: str) -> List[Any]:
        return [cls.from_string(p) for p in string.split(",") if p]


class PackageType(enum.Enum):
    rpm = 1
    deb = 2
    pkg = 3
    ebuild = 5


class Verb(enum.Enum):
    build   = "build"
    clean   = "clean"
    summary = "summary"
    shell   = "shell"
    boot    = "boot"
    qemu    = "qemu"
    ssh     = "ssh"
    serve   = "serve"
    bump    = "bump"
    help    = "help"
    genkey  = "genkey"

    # Defining __str__ is required to get "print_help()" output to include the human readable (values) of Verb.
    def __str__(self) -> str:
        return self.value


class Distribution(enum.Enum):
    package_type: PackageType

    fedora       = "fedora", PackageType.rpm
    debian       = "debian", PackageType.deb
    ubuntu       = "ubuntu", PackageType.deb
    arch         = "arch", PackageType.pkg
    opensuse     = "opensuse", PackageType.rpm
    mageia       = "mageia", PackageType.rpm
    centos       = "centos", PackageType.rpm
    centos_epel  = "centos_epel", PackageType.rpm
    openmandriva = "openmandriva", PackageType.rpm
    rocky        = "rocky", PackageType.rpm
    rocky_epel   = "rocky_epel", PackageType.rpm
    alma         = "alma", PackageType.rpm
    alma_epel    = "alma_epel", PackageType.rpm
    gentoo       = "gentoo", PackageType.ebuild

    def __new__(cls, name: str, package_type: PackageType) -> "Distribution":
        # This turns the list above into enum entries with .package_type attributes.
        # See https://docs.python.org/3.9/library/enum.html#when-to-use-new-vs-init
        # for an explanation.
        entry = object.__new__(cls)
        entry._value_ = name
        entry.package_type = package_type
        return entry

    def __str__(self) -> str:
        return self.name


def dictify(f: Callable[..., Iterator[Tuple[T, V]]]) -> Callable[..., Dict[T, V]]:
    def wrapper(*args: Any, **kwargs: Any) -> Dict[T, V]:
        return dict(f(*args, **kwargs))

    return functools.update_wrapper(wrapper, f)


@dictify
def read_os_release() -> Iterator[Tuple[str, str]]:
    try:
        filename = "/etc/os-release"
        f = open(filename)
    except FileNotFoundError:
        filename = "/usr/lib/os-release"
        f = open(filename)

    with f:
        for line_number, line in enumerate(f, start=1):
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"([A-Z][A-Z_0-9]+)=(.*)", line)
            if m:
                name, val = m.groups()
                if val and val[0] in "\"'":
                    val = ast.literal_eval(val)
                yield name, val
            else:
                print(f"{filename}:{line_number}: bad line {line!r}", file=sys.stderr)


def detect_distribution() -> Tuple[Optional[Distribution], Optional[str]]:
    try:
        os_release = read_os_release()
    except FileNotFoundError:
        return None, None

    dist_id = os_release.get("ID", "linux")
    dist_id_like = os_release.get("ID_LIKE", "").split()
    version = os_release.get("VERSION", None)
    version_id = os_release.get("VERSION_ID", None)
    version_codename = os_release.get("VERSION_CODENAME", None)
    extracted_codename = None

    if version:
        # extract Debian release codename
        m = re.search(r"\((.*?)\)", version)
        if m:
            extracted_codename = m.group(1)

    d: Optional[Distribution] = None
    for the_id in [dist_id, *dist_id_like]:
        d = Distribution.__members__.get(the_id, None)
        if d is not None:
            break

    if d in {Distribution.debian, Distribution.ubuntu} and (version_codename or extracted_codename):
        # debootstrap needs release codenames, not version numbers
        version_id = version_codename or extracted_codename

    return d, version_id


def is_rpm_distribution(d: Distribution) -> bool:
    return d in (
        Distribution.fedora,
        Distribution.mageia,
        Distribution.centos,
        Distribution.centos_epel,
        Distribution.openmandriva,
        Distribution.rocky,
        Distribution.rocky_epel,
        Distribution.alma,
        Distribution.alma_epel
    )


def is_centos_variant(d: Distribution) -> bool:
    return d in (
        Distribution.centos,
        Distribution.centos_epel,
        Distribution.alma,
        Distribution.alma_epel,
        Distribution.rocky,
        Distribution.rocky_epel,
    )


def is_epel_variant(d: Distribution) -> bool:
    return d in (
        Distribution.centos_epel,
        Distribution.alma_epel,
        Distribution.rocky_epel,
    )


class SourceFileTransfer(enum.Enum):
    copy_all = "copy-all"
    copy_git_cached = "copy-git-cached"
    copy_git_others = "copy-git-others"
    copy_git_more = "copy-git-more"
    mount = "mount"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def doc(cls) -> Dict[SourceFileTransfer, str]:
        return {
            cls.copy_all: "normal file copy",
            cls.copy_git_cached: "use git ls-files --cached, ignoring any file that git itself ignores",
            cls.copy_git_others: "use git ls-files --others, ignoring any file that git itself ignores",
            cls.copy_git_more: "use git ls-files --cached, ignoring any file that git itself ignores, but include the .git/ directory",
            cls.mount: "bind mount source files into the build image",
        }


class OutputFormat(Parseable, enum.Enum):
    directory = enum.auto()
    subvolume = enum.auto()
    tar = enum.auto()
    cpio = enum.auto()

    gpt_ext4 = enum.auto()
    gpt_xfs = enum.auto()
    gpt_btrfs = enum.auto()
    gpt_squashfs = enum.auto()

    plain_squashfs = enum.auto()

    # Kept for backwards compatibility
    raw_ext4 = raw_gpt = gpt_ext4
    raw_xfs = gpt_xfs
    raw_btrfs = gpt_btrfs
    raw_squashfs = gpt_squashfs

    def is_disk_rw(self) -> bool:
        "Output format is a disk image with a parition table and a writable filesystem"
        return self in (OutputFormat.gpt_ext4, OutputFormat.gpt_xfs, OutputFormat.gpt_btrfs)

    def is_disk(self) -> bool:
        "Output format is a disk image with a partition table"
        return self.is_disk_rw() or self == OutputFormat.gpt_squashfs

    def is_squashfs(self) -> bool:
        "The output format contains a squashfs partition"
        return self in {OutputFormat.gpt_squashfs, OutputFormat.plain_squashfs}

    def is_btrfs(self) -> bool:
        "The output format contains a btrfs partition"
        return self in {OutputFormat.gpt_btrfs, OutputFormat.subvolume}

    def can_minimize(self) -> bool:
        "The output format can be 'minimized'"
        return self in (OutputFormat.gpt_ext4, OutputFormat.gpt_btrfs)

    def needed_kernel_module(self) -> str:
        if self == OutputFormat.gpt_btrfs:
            return "btrfs"
        elif self in (OutputFormat.gpt_squashfs, OutputFormat.plain_squashfs):
            return "squashfs"
        elif self == OutputFormat.gpt_xfs:
            return "xfs"
        else:
            return "ext4"

    def has_fs_compression(self) -> bool:
        return self.is_squashfs() or self.is_btrfs()

    def __str__(self) -> str:
        return Parseable.__str__(self)

class ManifestFormat(Parseable, enum.Enum):
    json      = "json"       # the standard manifest in json format
    changelog = "changelog"  # human-readable text file with package changelogs

    def __str__(self) -> str:
        return Parseable.__str__(self)

class PartitionIdentifier(enum.Enum):
    esp        = "esp"
    bios       = "bios"
    xbootldr   = "xbootldr"
    root       = "root"
    swap       = "swap"
    home       = "home"
    srv        = "srv"
    var        = "var"
    tmp        = "tmp"
    verity     = "verity"
    verity_sig = "verity-sig"


@dataclasses.dataclass
class Partition:
    number: int

    n_sectors: int
    type_uuid: uuid.UUID
    part_uuid: Optional[uuid.UUID]
    read_only: Optional[bool]

    description: str

    def blockdev(self, loopdev: Path) -> Path:
        return Path(f"{loopdev}p{self.number}")

    def sfdisk_spec(self) -> str:
        desc = [f'size={self.n_sectors}',
                f'type={self.type_uuid}',
                f'attrs={"GUID:60" if self.read_only else ""}',
                f'name="{self.description}"',
                f'uuid={self.part_uuid}' if self.part_uuid is not None else None]
        return ', '.join(filter(None, desc))


@dataclasses.dataclass
class PartitionTable:
    partitions: Dict[PartitionIdentifier, Partition] = dataclasses.field(default_factory=dict)
    last_partition_sector: Optional[int] = None
    sector_size: int = 512
    first_lba: Optional[int] = None

    grain: int = 4096

    def first_partition_offset(self, max_partitions: int = 128) -> int:
        if self.first_lba is not None:
            # No rounding here, we honour the specified value exactly.
            return self.first_lba * self.sector_size
        else:
            # The header is like the footer, but we have a one-sector "protective MBR" at offset 0
            return roundup(self.sector_size + self.footer_size(), self.grain)

    def last_partition_offset(self, max_partitions: int = 128) -> int:
        if self.last_partition_sector:
            return roundup(self.last_partition_sector * self.sector_size, self.grain)
        else:
            return self.first_partition_offset(max_partitions)

    def partition_offset(self, partition: Partition) -> int:
        offset = self.first_partition_offset()

        for p in self.partitions.values():
            if p == partition:
                break

            offset += p.n_sectors * self.sector_size

        return offset

    def partition_size(self, partition: Partition) -> int:
        return partition.n_sectors * self.sector_size

    def footer_size(self, max_partitions: int = 128) -> int:
        # The footer must have enough space for the GPT header (one sector),
        # and the GPT parition entry area. PEA size of 16384 (128 partitions)
        # is recommended.
        pea_sectors = math.ceil(max_partitions * 128 / self.sector_size)
        return (1 + pea_sectors) * self.sector_size

    def disk_size(self) -> int:
        return roundup(self.last_partition_offset() + self.footer_size(), self.grain)

    def add(self,
            ident: PartitionIdentifier,
            size: int,
            type_uuid: uuid.UUID,
            description: str,
            part_uuid: Optional[uuid.UUID] = None,
            read_only: Optional[bool] = False) -> Partition:

        assert '"' not in description

        size = roundup(size, self.grain)
        n_sectors = size // self.sector_size

        part = Partition(len(self.partitions) + 1,
                         n_sectors, type_uuid, part_uuid, read_only, description)
        self.partitions[ident] = part

        self.last_partition_sector = self.last_partition_offset() // self.sector_size + n_sectors

        return part

    def partition_path(self, ident: PartitionIdentifier, loopdev: Path) -> Optional[Path]:
        part = self.partitions.get(ident)
        if part is None:
            return None

        return part.blockdev(loopdev)

    def sfdisk_spec(self) -> str:
        table = ["label: gpt",
                 f"grain: {self.grain}",
                 f"first-lba: {self.first_partition_offset() // self.sector_size}",
                 *(p.sfdisk_spec() for p in self.partitions.values())]
        return '\n'.join(table)

    def run_sfdisk(self, device: PathString, *, quiet: bool = False) -> None:
        spec = self.sfdisk_spec()
        device = Path(device)

        if 'disk' in ARG_DEBUG:
            print_between_lines(spec)

        cmd: List[PathString] = ["sfdisk", "--color=never", "--no-reread", "--no-tell-kernel", device]
        if quiet:
            cmd += ["--quiet"]

        if device.is_block_device():
            with open(device, 'rb+') as f:
                for p in self.partitions.values():
                    try:
                        blkpg_del_partition(f.fileno(), p.number)
                    except OSError as e:
                        if e.errno != errno.ENXIO:
                            raise

        try:
            run(cmd, input=spec.encode("utf-8"))
        except subprocess.CalledProcessError:
            print_between_lines(spec)
            raise

        if device.is_block_device():
            run(["sync"])

            # Make sure we re-add all partitions after modifying the partition table.
            with open(device, 'rb+') as f:
                for p in self.partitions.values():
                    blkpg_add_partition(f.fileno(), p.number, self.partition_offset(p), self.partition_size(p))

                try:
                    block_reread_partition_table(f.fileno())
                except OSError as e:
                    msg = f"Failed to reread partition table of {device}: {e.strerror}"
                    # BLKRRPART fails with EINVAL if the operation is not supported, let's not fail if that's
                    # the case.
                    if e.errno == errno.EINVAL:
                        warn(msg)
                    else:
                        die(msg)


@dataclasses.dataclass(frozen=True)
class MkosiConfig:
    """Type-hinted storage for command line arguments.

    Only user configuration is stored here while dynamic state exists in
    MkosiState. If a field of the same name exists in both classes always
    access the value from state.
    """

    verb: Verb
    cmdline: List[str]
    force: int

    distribution: Distribution
    release: str
    mirror: Optional[str]
    local_mirror: Optional[str]
    repository_key_check: bool
    repositories: List[str]
    use_host_repositories: bool
    repos_dir: Optional[str]
    architecture: str
    output_format: OutputFormat
    manifest_format: List[ManifestFormat]
    output: Path
    output_dir: Optional[Path]
    bootable: bool
    kernel_command_line: List[str]
    secure_boot: bool
    secure_boot_key: Path
    secure_boot_certificate: Path
    secure_boot_valid_days: str
    secure_boot_common_name: str
    read_only: bool
    encrypt: Optional[str]
    verity: Union[bool, str]
    sign_expected_pcr: bool
    compress: Union[None, str, bool]
    compress_fs: Union[None, str, bool]
    compress_output: Union[None, str, bool]
    mksquashfs_tool: List[PathString]
    qcow2: bool
    image_version: Optional[str]
    image_id: Optional[str]
    hostname: Optional[str]
    no_chown: bool
    tar_strip_selinux_context: bool
    incremental: bool
    minimize: bool
    with_unified_kernel_images: bool
    gpt_first_lba: Optional[int]
    hostonly_initrd: bool
    cache_initrd: bool
    base_packages: Union[str, bool]
    packages: List[str]
    remove_packages: List[str]
    with_docs: bool
    with_tests: bool
    cache_path: Path
    extra_trees: List[Path]
    skeleton_trees: List[Path]
    clean_package_metadata: Union[bool, str]
    remove_files: List[Path]
    environment: Dict[str, str]
    build_sources: Optional[Path]
    build_dir: Optional[Path]
    include_dir: Optional[Path]
    install_dir: Optional[Path]
    build_packages: List[str]
    skip_final_phase: bool
    build_script: Optional[Path]
    prepare_script: Optional[Path]
    postinst_script: Optional[Path]
    finalize_script: Optional[Path]
    source_file_transfer: SourceFileTransfer
    source_file_transfer_final: Optional[SourceFileTransfer]
    source_resolve_symlinks: bool
    source_resolve_symlinks_final: bool
    with_network: Union[bool, str]
    nspawn_settings: Optional[Path]
    base_image: Optional[Path]
    root_size: int
    esp_size: int
    xbootldr_size: int
    swap_size: int
    home_size: int
    srv_size: int
    var_size: int
    tmp_size: int
    bios_size: int
    usr_only: bool
    split_artifacts: bool
    checksum: bool
    sign: bool
    key: Optional[str]
    bmap: bool
    password: Optional[str]
    password_is_hashed: bool
    autologin: bool
    extra_search_paths: List[Path]
    netdev: bool
    ephemeral: bool
    ssh: bool
    ssh_key: Optional[Path]
    ssh_agent: Optional[Path]
    ssh_timeout: int
    ssh_port: int
    directory: Optional[Path]
    config_path: Optional[Path]
    all: bool
    all_directory: Optional[Path]
    debug: List[str]
    auto_bump: bool
    workspace_dir: Optional[Path]
    machine_id: Optional[str]

    # QEMU-specific options
    qemu_headless: bool
    qemu_smp: str
    qemu_mem: str
    qemu_kvm: bool
    qemu_args: Sequence[str]
    qemu_boot: str

    # systemd-nspawn specific options
    nspawn_keep_unit: bool

    passphrase: Optional[Dict[str, str]]

    output_checksum: Optional[Path] = None
    output_nspawn_settings: Optional[Path] = None
    output_sshkey: Optional[Path] = None
    output_root_hash_file: Optional[Path] = None
    output_root_hash_p7s_file: Optional[Path] = None
    output_bmap: Optional[Path] = None
    output_split_root: Optional[Path] = None
    output_split_verity: Optional[Path] = None
    output_split_verity_sig: Optional[Path] = None
    output_split_kernel: Optional[Path] = None
    output_signature: Optional[Path] = None

    def architecture_is_native(self) -> bool:
        return self.architecture == platform.machine()


@dataclasses.dataclass
class MkosiState:
    """State related properties."""

    config: MkosiConfig
    workspace: Path
    cache: Path
    do_run_build_script: bool
    machine_id: str
    for_cache: bool
    environment: Dict[str, str] = dataclasses.field(init=False)
    installer: DistributionInstaller = dataclasses.field(init=False)

    cache_pre_inst: Optional[Path] = None
    cache_pre_dev: Optional[Path] = None

    partition_table: Optional[PartitionTable] = None

    def __post_init__(self) -> None:
        self.environment = self.config.environment.copy()
        if self.config.image_id is not None:
            self.environment['IMAGE_ID'] = self.config.image_id
        if self.config.image_version is not None:
            self.environment['IMAGE_VERSION'] = self.config.image_version
        try:
            distro = str(self.config.distribution)
            mod = importlib.import_module(f"mkosi.distributions.{distro}")
            installer = getattr(mod, f"{distro.title().replace('_','')}Installer")
            instance = installer() if issubclass(installer, DistributionInstaller) else None
        except (ImportError, AttributeError):
            instance = None
        if instance is None:
            die("No installer for this distribution.")
        self.installer = instance

    @property
    def root(self) -> Path:
        return self.workspace / "root"

    def var_tmp(self) -> Path:
        p = self.workspace / "var-tmp"
        p.mkdir(exist_ok=True)
        return p

    def get_partition(self, ident: PartitionIdentifier) -> Optional[Partition]:
        "A shortcut to check that we have a partition table and extract the partition object"
        if self.partition_table is None:
            return None
        return self.partition_table.partitions.get(ident)


def should_compress_fs(config: Union[argparse.Namespace, MkosiConfig]) -> Union[bool, str]:
    """True for the default compression, a string, or False.

    When explicitly configured with --compress-fs=, just return
    whatever was specified. When --compress= was used, try to be
    smart, so that either this function or should_compress_output()
    returns True as appropriate.
    """
    c = config.compress_fs
    if c is None and config.output_format.has_fs_compression():
        c = config.compress
    return False if c is None else c


def should_compress_output(config: Union[argparse.Namespace, MkosiConfig]) -> Union[bool, str]:
    """A string or False.

    When explicitly configured with --compress-output=, use
    that. Since we have complete freedom with selecting the outer
    compression algorithm, pick some default when True. When
    --compress= was used, try to be smart, so that either this
    function or should_compress_fs() returns True as appropriate.
    """
    c = config.compress_output
    if c is None and not config.output_format.has_fs_compression():
        c = config.compress
    if c is None and config.output_format == OutputFormat.tar:
        c = True
    if c is True:
        return "xz"  # default compression
    return False if c is None else c


def workspace(root: Path) -> Path:
    return root.parent


def nspawn_knows_arg(arg: str) -> bool:
    # Specify some extra incompatible options so nspawn doesn't try to boot a container in the current
    # directory if it has a compatible layout.
    return "unrecognized option" not in run(["systemd-nspawn", arg,
                                            "--directory", "/dev/null", "--image", "/dev/null"],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
                                            text=True).stderr


def format_rlimit(rlimit: int) -> str:
    limits = resource.getrlimit(rlimit)
    soft = "infinity" if limits[0] == resource.RLIM_INFINITY else str(limits[0])
    hard = "infinity" if limits[1] == resource.RLIM_INFINITY else str(limits[1])
    return f"{soft}:{hard}"


def nspawn_rlimit_params() -> Sequence[str]:
    return [
        f"--rlimit=RLIMIT_CORE={format_rlimit(resource.RLIMIT_CORE)}",
    ] if nspawn_knows_arg("--rlimit") else []


def nspawn_version() -> int:
    return int(run(["systemd-nspawn", "--version"], stdout=subprocess.PIPE).stdout.strip().split()[1])


def run_workspace_command(
    state: MkosiState,
    cmd: Sequence[PathString],
    network: bool = False,
    env: Optional[Mapping[str, str]] = None,
    nspawn_params: Optional[List[str]] = None,
    capture_stdout: bool = False,
    check: bool = True,
) -> CompletedProcess:
    nspawn = [
        "systemd-nspawn",
        "--quiet",
        f"--directory={state.root}",
        "--machine=mkosi-" + uuid.uuid4().hex,
        "--as-pid2",
        "--link-journal=no",
        "--register=no",
        f"--bind={state.var_tmp()}:/var/tmp",
        "--setenv=SYSTEMD_OFFLINE=1",
        *nspawn_rlimit_params(),
    ]
    stdout = None

    if network:
        # If we're using the host network namespace, use the same resolver
        nspawn += ["--bind-ro=/etc/resolv.conf"]
    else:
        nspawn += ["--private-network"]

    if env:
        nspawn += [f"--setenv={k}={v}" for k, v in env.items()]
    if "workspace-command" in ARG_DEBUG:
        nspawn += ["--setenv=SYSTEMD_LOG_LEVEL=debug"]

    if nspawn_params:
        nspawn += nspawn_params

    if capture_stdout:
        stdout = subprocess.PIPE
        nspawn += ["--console=pipe"]

    if state.config.usr_only:
        nspawn += [f"--bind={root_home(state)}:/root"]

    if state.config.nspawn_keep_unit:
        nspawn += ["--keep-unit"]

    try:
        return run([*nspawn, "--", *cmd], check=check, stdout=stdout, text=capture_stdout)
    except subprocess.CalledProcessError as e:
        if "workspace-command" in ARG_DEBUG:
            run(nspawn, check=False)
        die(f"Workspace command {shell_join(cmd)} returned non-zero exit code {e.returncode}.")


def root_home(state: MkosiState) -> Path:

    # If UsrOnly= is turned on the /root/ directory (i.e. the root
    # user's home directory) is not persistent (after all everything
    # outside of /usr/ is not around). In that case let's mount it in
    # from an external place, so that we can have persistency. It is
    # after all where we place our build sources and suchlike.

    if state.config.usr_only:
        return state.workspace / "home-root"

    return state.root / "root"


@contextlib.contextmanager
def do_delay_interrupt() -> Iterator[None]:
    # CTRL+C is sent to the entire process group. We delay its handling in mkosi itself so the subprocess can
    # exit cleanly before doing mkosi's cleanup. If we don't do this, we get device or resource is busy
    # errors when unmounting stuff later on during cleanup. We only delay a single CTRL+C interrupt so that a
    # user can always exit mkosi even if a subprocess hangs by pressing CTRL+C twice.
    interrupted = False

    def handler(signal: int, frame: Optional[FrameType]) -> None:
        nonlocal interrupted
        if interrupted:
            raise KeyboardInterrupt()
        else:
            interrupted = True

    s = signal.signal(signal.SIGINT, handler)

    try:
        yield
    finally:
        signal.signal(signal.SIGINT, s)

        if interrupted:
            die("Interrupted")


@contextlib.contextmanager
def do_noop() -> Iterator[None]:
    yield


# Borrowed from https://github.com/python/typeshed/blob/3d14016085aed8bcf0cf67e9e5a70790ce1ad8ea/stdlib/3/subprocess.pyi#L24
_FILE = Union[None, int, IO[Any]]


def spawn(
    cmdline: Sequence[PathString],
    delay_interrupt: bool = True,
    stdout: _FILE = None,
    stderr: _FILE = None,
    **kwargs: Any,
) -> Popen:
    if "run" in ARG_DEBUG:
        MkosiPrinter.info(f"+ {shell_join(cmdline)}")

    if not stdout and not stderr:
        # Unless explicit redirection is done, print all subprocess
        # output on stderr, since we do so as well for mkosi's own
        # output.
        stdout = sys.stderr

    cm = do_delay_interrupt if delay_interrupt else do_noop
    try:
        with cm():
            return subprocess.Popen(cmdline, stdout=stdout, stderr=stderr, **kwargs)
    except FileNotFoundError:
        die(f"{cmdline[0]} not found in PATH.")


def run(
    cmdline: Sequence[PathString],
    check: bool = True,
    delay_interrupt: bool = True,
    stdout: _FILE = None,
    stderr: _FILE = None,
    env: Mapping[str, Any] = {},
    **kwargs: Any,
) -> CompletedProcess:
    cmdline = [os.fspath(x) for x in cmdline]

    if "run" in ARG_DEBUG:
        MkosiPrinter.info(f"+ {shell_join(cmdline)}")

    if not stdout and not stderr:
        # Unless explicit redirection is done, print all subprocess
        # output on stderr, since we do so as well for mkosi's own
        # output.
        stdout = sys.stderr

    # This is a workaround for copy_git_files, which uses the user= option to
    # subprocess.run, which is only available starting with Python 3.9
    # TODO: remove this branch once mkosi defaults to at least Python 3.9
    if "user" in kwargs and sys.version_info < (3, 9):
        user = kwargs.pop("user")
        user = f"#{user}" if isinstance(user, int) else user
        cmdline = ["sudo", "-u", user] + cmdline

    cm = do_delay_interrupt if delay_interrupt else do_noop
    try:
        with cm():
            return subprocess.run(cmdline, check=check, stdout=stdout, stderr=stderr, env={**os.environ, **env}, **kwargs)
    except FileNotFoundError:
        die(f"{cmdline[0]} not found in PATH.")


def tmp_dir() -> Path:
    path = os.environ.get("TMPDIR") or "/var/tmp"
    return Path(path)


def patch_file(filepath: Path, line_rewriter: Callable[[str], str]) -> None:
    temp_new_filepath = filepath.with_suffix(filepath.suffix + ".tmp.new")

    with filepath.open("r") as old, temp_new_filepath.open("w") as new:
        for line in old:
            new.write(line_rewriter(line))

    shutil.copystat(filepath, temp_new_filepath)
    os.remove(filepath)
    shutil.move(str(temp_new_filepath), filepath)


def path_relative_to_cwd(path: PathString) -> Path:
    "Return path as relative to $PWD if underneath, absolute path otherwise"
    path = Path(path)

    try:
        return path.relative_to(os.getcwd())
    except ValueError:
        return path


def die(message: str, exception: Type[MkosiException] = MkosiException) -> NoReturn:
    MkosiPrinter.warn(f"Error: {message}")
    raise exception(message)


def warn(message: str) -> None:
    MkosiPrinter.warn(f"Warning: {message}")


class MkosiPrinter:
    out_file = sys.stderr
    isatty = out_file.isatty()

    bold = "\033[0;1;39m" if isatty else ""
    red = "\033[31;1m" if isatty else ""
    reset = "\033[0m" if isatty else ""

    prefix = "‣ "

    level = 0

    @classmethod
    def _print(cls, text: str) -> None:
        cls.out_file.write(text)

    @classmethod
    def color_error(cls, text: Any) -> str:
        return f"{cls.red}{text}{cls.reset}"

    @classmethod
    def print_step(cls, text: str) -> None:
        prefix = cls.prefix + " " * cls.level
        if sys.exc_info()[0]:
            # We are falling through exception handling blocks.
            # De-emphasize this step here, so the user can tell more
            # easily which step generated the exception. The exception
            # or error will only be printed after we finish cleanup.
            cls._print(f"{prefix}({text})\n")
        else:
            cls._print(f"{prefix}{cls.bold}{text}{cls.reset}\n")

    @classmethod
    def info(cls, text: str) -> None:
        cls._print(text + "\n")

    @classmethod
    def warn(cls, text: str) -> None:
        cls._print(f"{cls.prefix}{cls.color_error(text)}\n")

    @classmethod
    @contextlib.contextmanager
    def complete_step(cls, text: str, text2: Optional[str] = None) -> Iterator[List[Any]]:
        cls.print_step(text)

        cls.level += 1
        try:
            args: List[Any] = []
            yield args
        finally:
            cls.level -= 1
            assert cls.level >= 0

        if text2 is not None:
            cls.print_step(text2.format(*args))


def chown_to_running_user(path: PathString) -> None:
    uid = int(os.getenv("SUDO_UID") or os.getenv("PKEXEC_UID") or str(os.getuid()))
    user = pwd.getpwuid(uid).pw_name
    gid = pwd.getpwuid(uid).pw_gid

    with MkosiPrinter.complete_step(
        f"Changing ownership of output file {path} to user {user}…",
        f"Changed ownership of {path}",
    ):
        os.chown(path, uid, gid)


def mkdirp_chown_current_user(
    path: PathString,
    *,
    skip_chown: bool = False,
    mode: int = 0o777,
    exist_ok: bool = True
) -> None:
    abspath = Path(path).absolute()
    path = Path()

    for d in abspath.parts:
        path /= d
        if path.exists():
            continue

        path.mkdir(mode=mode, exist_ok=exist_ok)

        if skip_chown:
            continue

        chown_to_running_user(path)


def safe_tar_extract(tar: tarfile.TarFile, path: Path=Path("."), *, numeric_owner: bool=False) -> None:
    """Extract a tar without CVE-2007-4559.

    Throws a MkosiException if a member of the tar resolves to a path that would
    be outside of the passed in target path.

    Omits the member argument from TarFile.extractall, since we don't need it at
    the moment.

    See https://github.com/advisories/GHSA-gw9q-c7gh-j9vm
    """
    path = path.resolve()
    for member in tar.getmembers():
        target = path / member.name
        try:
            # a.relative_to(b) throws a ValueError if a is not a subpath of b
            target.resolve().relative_to(path)
        except ValueError as e:
            raise MkosiException(f"Attempted path traversal in tar file {tar.name!r}") from e

    tar.extractall(path, numeric_owner=numeric_owner)


complete_step = MkosiPrinter.complete_step


def disable_pam_securetty(root: Path) -> None:
    def _rm_securetty(line: str) -> str:
        if "pam_securetty.so" in line:
            return ""
        return line

    patch_file(root / "etc/pam.d/login", _rm_securetty)


def add_packages(
    config: MkosiConfig, packages: Set[str], *names: str, conditional: Optional[str] = None
) -> None:

    """Add packages in @names to @packages, if enabled by --base-packages.

    If @conditional is specified, rpm-specific syntax for boolean
    dependencies will be used to include @names if @conditional is
    satisfied.
    """
    assert config.base_packages is True or config.base_packages is False or config.base_packages == "conditional"

    if config.base_packages is True or (config.base_packages == "conditional" and conditional):
        for name in names:
            packages.add(f"({name} if {conditional})" if conditional else name)


def sort_packages(packages: Iterable[str]) -> List[str]:
    """Sorts packages: normal first, paths second, conditional third"""

    m = {"(": 2, "/": 1}
    sort = lambda name: (m.get(name[0], 0), name)
    return sorted(packages, key=sort)


def scandir_recursive(
    root: Path,
    filter: Optional[Callable[[os.DirEntry[str]], T]] = None,
) -> Iterator[T]:
    """Recursively walk the tree starting at @root, optionally apply filter, yield non-none values"""
    queue: Deque[Union[str, Path]] = collections.deque([root])

    while queue:
        for entry in os.scandir(queue.pop()):
            pred = filter(entry) if filter is not None else entry
            if pred is not None:
                yield cast(T, pred)
            if entry.is_dir(follow_symlinks=False):
                queue.append(entry.path)
