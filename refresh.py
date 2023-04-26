"""

Interface:
- `bazel run` to create or validate that a symlink to /external exists, which is referenced
        in the generated compile_commands.json. Only needs to be run once on initialization
    - Requires being run under Bazel so we can access the workspace root environment variable.
"""

import sys
if sys.version_info < (3,6):
    sys.exit("\n\033[31mFATAL ERROR:\033[0m Python 3.6 or later is required. Please update!")
    # 3.6 backwards compatibility required by @zhanyong-wan in https://github.com/hedronvision/bazel-compile-commands-extractor/issues/111.
    # 3.7 backwards compatibility required by @lummax in https://github.com/hedronvision/bazel-compile-commands-extractor/pull/27.
    # ^ Try to contact before upgrading.
    # When adding things could be cleaner if we had a higher minimum version, please add a comment with MIN_PY=3.<v>.
    # Similarly, when upgrading, please search for that MIN_PY= tag.


import enum
import locale
import os
import pathlib
import subprocess
import shutil

@enum.unique
class SGR(enum.Enum):
    """Enumerate (some of the) available SGR (Select Graphic Rendition) control sequences."""
    # For details on SGR control sequences (and ANSI escape codes in general), see: https://en.wikipedia.org/wiki/ANSI_escape_code#SGR_(Select_Graphic_Rendition)_parameters
    RESET = '\033[0m'
    FG_RED = '\033[0;31m'
    FG_GREEN = '\033[0;32m'
    FG_YELLOW = '\033[0;33m'
    FG_BLUE = '\033[0;34m'


def _log_with_sgr(sgr, colored_message, uncolored_message=''):
    """Log a message to stderr wrapped in an SGR context."""
    print(sgr.value, colored_message, SGR.RESET.value, uncolored_message, sep='', file=sys.stderr, flush=True)


def log_error(colored_message, uncolored_message=''):
    """Log an error message (in red) to stderr."""
    _log_with_sgr(SGR.FG_RED, colored_message, uncolored_message)


def log_warning(colored_message, uncolored_message=''):
    """Log a warning message (in yellow) to stderr."""
    _log_with_sgr(SGR.FG_YELLOW, colored_message, uncolored_message)


def log_info(colored_message, uncolored_message=''):
    """Log an informative message (in blue) to stderr."""
    _log_with_sgr(SGR.FG_BLUE, colored_message, uncolored_message)


def log_success(colored_message, uncolored_message=''):
    """Log a success message (in green) to stderr."""
    _log_with_sgr(SGR.FG_GREEN, colored_message, uncolored_message)


def _ensure_external_workspaces_link_exists():
    """Postcondition: Either //external points into Bazel's fullest set of external workspaces in output_base, or we've exited with an error that'll help the user resolve the issue."""
    is_windows = os.name == 'nt'
    source = pathlib.Path('external')

    if not os.path.lexists('bazel-out'):
        log_error(">>> //bazel-out is missing. Please remove --symlink_prefix and --experimental_convenience_symlinks, so the workspace mirrors the compilation environment.")
        # Crossref: https://github.com/hedronvision/bazel-compile-commands-extractor/issues/14 https://github.com/hedronvision/bazel-compile-commands-extractor/pull/65
        # Note: experimental_no_product_name_out_symlink is now enabled by default. See https://github.com/bazelbuild/bazel/commit/06bd3e8c0cd390f077303be682e9dec7baf17af2
        sys.exit(1)

    # Traverse into output_base via bazel-out, keeping the workspace position-independent, so it can be moved without rerunning
    dest = pathlib.Path('bazel-out/../../../external')
    if is_windows:
        # On Windows, unfortunately, bazel-out is a junction, and acessing .. of a junction brings you back out the way you came. So we have to resolve bazel-out first. Not position-independent, but I think the best we can do
        dest = (pathlib.Path('bazel-out').resolve()/'../../../external').resolve()

    # Handle problem cases where //external exists
    if os.path.lexists(source):
        # Detect symlinks or Windows junctions
        # This seemed to be the cleanest way to detect both.
        # Note that os.path.islink doesn't detect junctions.
        try:
            current_dest = os.readlink(source) # MIN_PY=3.9 source.readlink()
        except OSError:
            log_error(f">>> //external already exists, but it isn't a {'junction' if is_windows else 'symlink'}. //external is reserved by Bazel and needed for this tool. Please rename or delete your existing //external and rerun. More details in the README if you want them.") # Don't auto delete in case the user has something important there.
            sys.exit(1)

        # Normalize the path for matching
        # First, workaround a gross case where Windows readlink returns extended path, starting with \\?\, causing the match to fail
        if is_windows and current_dest.startswith('\\\\?\\'):
            current_dest = current_dest[4:] # MIN_PY=3.9 stripprefix
        current_dest = pathlib.Path(current_dest)

        if dest != current_dest:
            log_warning(">>> //external links to the wrong place. Automatically deleting and relinking...")
            source.unlink()

    # Create link if it doesn't already exist
    if not os.path.lexists(source):
        if is_windows:
            # We create a junction on Windows because symlinks need more than default permissions (ugh). Without an elevated prompt or a system in developer mode, symlinking would fail with get "OSError: [WinError 1314] A required privilege is not held by the client:"
            subprocess.run(f'mklink /J "{source}" "{dest}"', check=True, shell=True) # shell required for mklink builtin
        else:
            source.symlink_to(dest, target_is_directory=True)
        log_success(""">>> Automatically added //external workspace link:
    This link makes it easy for you--and for build tooling--to see the external dependencies you bring in. It also makes your source tree have the same directory structure as the build sandbox.
    It's a win/win: It's easier for you to browse the code you use, and it eliminates whole categories of edge cases for build tooling.""")


def _ensure_gitignore_entries_exist():
    """Ensure `//compile_commands.json`, `//external`, and other useful entries are `.gitignore`'d if in a git repo."""
    # Silently check if we're (nested) within a git repository. It isn't sufficient to check for the presence of a `.git` directory, in case, e.g., the bazel workspace is nested inside the git repository or you're off in a git worktree.
    git_dir_process = subprocess.run('git rev-parse --git-common-dir', # common-dir because despite current gitignore docs, there's just one info/exclude in the common git dir, not one in each of the worktree's git dirs.
        shell=True,  # Ensure this will still fail with a nonzero error code even if `git` isn't installed, unifying error cases.
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding=locale.getpreferredencoding(),
    )
    # A nonzero error code indicates that we are not (nested) within a git repository.
    if git_dir_process.returncode: return

    # Write into the gitignore hidden inside the .git directory
    # This makes ignoring work automagically for people, while minimizing the code changes they have to think about or check in. https://github.com/hedronvision/bazel-compile-commands-extractor/pull/100 and https://github.com/hedronvision/bazel-compile-commands-extractor/issues/59 are exampels of use cases that this simplifies. It also marginally simplifies the case where people can't commit use of this tool to the repo they're working on.
    # IMO tools should to do this more broadly, especially now that git is so dominant.
    # Hidden gitignore documented in https://git-scm.com/docs/gitignore
    git_dir = pathlib.Path(git_dir_process.stdout.rstrip())
    (git_dir / 'info').mkdir(exist_ok=True) # Some older git versions don't auto create .git/info/, creating an error on exclude file open. See https://github.com/hedronvision/bazel-compile-commands-extractor/issues/114 for more context. We'll create the .git/info/ if needed; the git docs don't guarantee its existance. (We could instead back to writing .gitignore in the repo and bazel workspace, but we don't because this case is rare and because future git versions would be within their rights to read .git/info/exclude but not auto-create .git/info/)
    hidden_gitignore_path = git_dir / 'info' / 'exclude'

    # Get path to the workspace root (current working directory) from the git repository root
    git_prefix_process = subprocess.run(['git', 'rev-parse', '--show-prefix'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding=locale.getpreferredencoding(),
        check=True, # Should always succeed if the other did
    )
    pattern_prefix = git_prefix_process.stdout.rstrip()

    # Each (pattern, explanation) will be added to the `.gitignore` file if the pattern isn't present.
    needed_entries = [
        (f'/{pattern_prefix}external', "# Ignore the `external` link (that is added by `bazel-compile-commands-extractor`). The link differs between macOS/Linux and Windows, so it shouldn't be checked in. The pattern must not end with a trailing `/` because it's a symlink on macOS/Linux."),
        (f'/{pattern_prefix}bazel-*', "# Ignore links to Bazel's output. The pattern needs the `*` because people can change the name of the directory into which your repository is cloned (changing the `bazel-<workspace_name>` symlink), and must not end with a trailing `/` because it's a symlink on macOS/Linux. This ignore pattern should almost certainly be checked into a .gitignore in your workspace root, too, for folks who don't use this tool."),
        (f'/{pattern_prefix}compile_commands.json', "# Ignore generated output. Although valuable (after all, the primary purpose of `bazel-compile-commands-extractor` is to produce `compile_commands.json`!), it should not be checked in."),
        ('.cache/', "# Ignore the directory in which `clangd` stores its local index."),
    ]

    # Create `.gitignore` if it doesn't exist (and don't truncate if it does) and open it for appending/updating.
    with open(hidden_gitignore_path, 'a+') as gitignore:
        gitignore.seek(0)  # Files opened in `a` mode seek to the end, so we reset to the beginning so we can read.
        # Recall that trailing spaces, when escaped with `\`, are meaningful to git. However, none of the entries for which we're searching end with literal spaces, so we can safely trim all trailing whitespace. That said, we can't rewrite these stripped lines to the file, in case an existing entry is e.g. `/foo\ `, matching the file "foo " (with a trailing space), whereas the entry `/foo\` does not match the file `"foo "`.
        lines = [l.rstrip() for l in gitignore]
        # Comments must be on their own line, so we can safely check for equality here.
        missing = [entry for entry in needed_entries if entry[0] not in lines]
        if not missing:
            return
        # Add a spacer before the header if the last line is nonempty.
        if lines and lines[-1]:
            print(file=gitignore)
        # Add a nice header.
        print("### Automatically added by Hedron's Bazel Compile Commands Extractor: https://github.com/hedronvision/bazel-compile-commands-extractor", file=gitignore)
        # Append the missing entries.
        for pattern, comment in missing:
            print(comment, file=gitignore)
            print(pattern, file=gitignore)
    log_success(">>> Automatically added entries to .git/info/exclude to gitignore generated output.")


def _ensure_cwd_is_workspace_root():
    """Set the current working directory to the root of the workspace."""
    # The `bazel run` command sets `BUILD_WORKSPACE_DIRECTORY` to "the root of the workspace where the build was run." See: https://bazel.build/docs/user-manual#running-executables.
    try:
        workspace_root = pathlib.Path(os.environ['BUILD_WORKSPACE_DIRECTORY'])
    except KeyError:
        log_error(">>> BUILD_WORKSPACE_DIRECTORY was not found in the environment. Make sure to invoke this tool with `bazel run`.")
        sys.exit(1)
    # Change the working directory to the workspace root (assumed by future commands).
    # Although this can fail (OSError/FileNotFoundError/PermissionError/NotADirectoryError), there's no easy way to recover, so we'll happily crash.
    os.chdir(workspace_root)


if __name__ == '__main__':
    _ensure_cwd_is_workspace_root()
    _ensure_gitignore_entries_exist()
    _ensure_external_workspaces_link_exists()

    shutil.copyfile("bazel-bin/compile_commands.json", "compile_commands.json")