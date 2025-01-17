# encoding: utf-8
"""
An application for IPython.

All top-level applications should use the classes in this module for
handling configuration and creating configurables.

The job of an :class:`Application` is to create the master configuration
object and then create the configurable objects, passing the config to them.
"""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.

import atexit
import glob
import logging
import os
import shutil
import sys

from traitlets.config.application import Application, catch_config_error
from traitlets.config.loader import ConfigFileNotFound, PyFileConfigLoader
from IPython.core import release, crashhandler
from IPython.core.profiledir import ProfileDir, ProfileDirError
from IPython.paths import get_ipython_dir, get_ipython_package_dir
from IPython.utils.path import ensure_dir_exists
from IPython.utils import py3compat
from traitlets import (
    List, Unicode, Type, Bool, Dict, Set, Instance, Undefined,
    default, observe,
)

if os.name == 'nt':
    programdata = os.environ.get('PROGRAMDATA', None)
    if programdata:
        SYSTEM_CONFIG_DIRS = [os.path.join(programdata, 'ipython')]
    else:  # PROGRAMDATA is not defined by default on XP.
        SYSTEM_CONFIG_DIRS = []
else:
    SYSTEM_CONFIG_DIRS = [
        "/usr/local/etc/ipython",
        "/etc/ipython",
    ]

_envvar = os.environ.get('IPYTHON_SUPPRESS_ERRORS')
if _envvar in {None, ''}:
    IPYTHON_SUPPRESS_ERRORS = None
else:
    if _envvar.lower() in {'1','true'}:
        IPYTHON_SUPPRESS_ERRORS = True
    elif _envvar.lower() in {'0','false'} :
        IPYTHON_SUPPRESS_ERRORS = False
    else:
        sys.exit("Unsupported value for environment variable: 'IPYTHON_SUPPRESS_ERRORS' is set to '%s' which is none of  {'0', '1', 'false', 'true', ''}."% _envvar )

# aliases and flags

base_aliases = {
    'profile-dir' : 'ProfileDir.location',
    'profile' : 'BaseIPythonApplication.profile',
    'ipython-dir' : 'BaseIPythonApplication.ipython_dir',
    'log-level' : 'Application.log_level',
    'config' : 'BaseIPythonApplication.extra_config_file',
}

base_flags = dict(
    debug = ({'Application' : {'log_level' : logging.DEBUG}},
            "set log level to logging.DEBUG (maximize logging output)"),
    quiet = ({'Application' : {'log_level' : logging.CRITICAL}},
            "set log level to logging.CRITICAL (minimize logging output)"),
    init = ({'BaseIPythonApplication' : {
                    'copy_config_files' : True,
                    'auto_create' : True}
            }, """Initialize profile with default config files.  This is equivalent
            to running `ipython profile create <profile>` prior to startup.
            """)
)

class ProfileAwareConfigLoader(PyFileConfigLoader):
    """A Python file config loader that is aware of IPython profiles."""
    def load_subconfig(self, fname, path=None, profile=None):
        if profile is not None:
            try:
                profile_dir = ProfileDir.find_profile_dir_by_name(
                        get_ipython_dir(),
                        profile,
                )
            except ProfileDirError:
                return
            path = profile_dir.location
        return super(ProfileAwareConfigLoader, self).load_subconfig(fname, path=path)

class BaseIPythonApplication(Application):

    name = Unicode(u'ipython')
    description = Unicode(u'IPython: an enhanced interactive Python shell.')
    version = Unicode(release.version)

    aliases = Dict(base_aliases)
    flags = Dict(base_flags)
    classes = List([ProfileDir])
    
    # enable `load_subconfig('cfg.py', profile='name')`
    python_config_loader_class = ProfileAwareConfigLoader

    # Track whether the config_file has changed,
    # because some logic happens only if we aren't using the default.
    config_file_specified = Set()

    config_file_name = Unicode()
    @default('config_file_name')
    def _config_file_name_default(self):
        return self.name.replace('-','_') + u'_config.py'
    @observe('config_file_name')
    def _config_file_name_changed(self, change):
        if change['new'] != change['old']:
            self.config_file_specified.add(change['new'])

    # The directory that contains IPython's builtin profiles.
    builtin_profile_dir = Unicode(
        os.path.join(get_ipython_package_dir(), u'config', u'profile', u'default')
    )
    
    config_file_paths = List(Unicode())
    @default('config_file_paths')
    def _config_file_paths_default(self):
        return [py3compat.getcwd()]

    extra_config_file = Unicode(
    help="""Path to an extra config file to load.
    
    If specified, load this config file in addition to any other IPython config.
    """).tag(config=True)
    @observe('extra_config_file')
    def _extra_config_file_changed(self, change):
        old = change['old']
        new = change['new']
        try:
            self.config_files.remove(old)
        except ValueError:
            pass
        self.config_file_specified.add(new)
        self.config_files.append(new)

    profile = Unicode(u'default',
        help="""The IPython profile to use."""
    ).tag(config=True)
    
    @observe('profile')
    def _profile_changed(self, change):
        self.builtin_profile_dir = os.path.join(
                get_ipython_package_dir(), u'config', u'profile', change['new']
        )

    ipython_dir = Unicode(
        help="""
        The name of the IPython directory. This directory is used for logging
        configuration (through profiles), history storage, etc. The default
        is usually $HOME/.ipython. This option can also be specified through
        the environment variable IPYTHONDIR.
        """
    ).tag(config=True)
    @default('ipython_dir')
    def _ipython_dir_default(self):
        d = get_ipython_dir()
        self._ipython_dir_changed({
            'name': 'ipython_dir',
            'old': d,
            'new': d,
        })
        return d
    
    _in_init_profile_dir = False
    profile_dir = Instance(ProfileDir, allow_none=True)
    @default('profile_dir')
    def _profile_dir_default(self):
        # avoid recursion
        if self._in_init_profile_dir:
            return
        # profile_dir requested early, force initialization
        self.init_profile_dir()
        return self.profile_dir

    overwrite = Bool(False,
        help="""Whether to overwrite existing config files when copying"""
    ).tag(config=True)
    auto_create = Bool(False,
        help="""Whether to create profile dir if it doesn't exist"""
    ).tag(config=True)

    config_files = List(Unicode())
    @default('config_files')
    def _config_files_default(self):
        return [self.config_file_name]

    copy_config_files = Bool(False,
        help="""Whether to install the default config files into the profile dir.
        If a new profile is being created, and IPython contains config files for that
        profile, then they will be staged into the new directory.  Otherwise,
        default config files will be automatically generated.
        """).tag(config=True)
    
    verbose_crash = Bool(False,
        help="""Create a massive crash report when IPython encounters what may be an
        internal error.  The default is to append a short message to the
        usual traceback""").tag(config=True)

    # The class to use as the crash handler.
    crash_handler_class = Type(crashhandler.CrashHandler)

    @catch_config_error
    def __init__(self, **kwargs):
        super(BaseIPythonApplication, self).__init__(**kwargs)
        # ensure current working directory exists
        try:
            py3compat.getcwd()
        except:
            # exit if cwd doesn't exist
            self.log.error("Current working directory doesn't exist.")
            self.exit(1)

    #-------------------------------------------------------------------------
    # Various stages of Application creation
    #-------------------------------------------------------------------------
    
    deprecated_subcommands = {}
    
    def initialize_subcommand(self, subc, argv=None):
        if subc in self.deprecated_subcommands:
            self.log.warning("Subcommand `ipython {sub}` is deprecated and will be removed "
                             "in future versions.".format(sub=subc))
            self.log.warning("You likely want to use `jupyter {sub}` in the "
                             "future".format(sub=subc))
        return super(BaseIPythonApplication, self).initialize_subcommand(subc, argv)

    def init_crash_handler(self):
        """Create a crash handler, typically setting sys.excepthook to it."""
        self.crash_handler = self.crash_handler_class(self)
        sys.excepthook = self.excepthook
        def unset_crashhandler():
            sys.excepthook = sys.__excepthook__
        atexit.register(unset_crashhandler)
    
    def excepthook(self, etype, evalue, tb):
        """this is sys.excepthook after init_crashhandler
        
        set self.verbose_crash=True to use our full crashhandler, instead of
        a regular traceback with a short message (crash_handler_lite)
        """
        
        if self.verbose_crash:
            return self.crash_handler(etype, evalue, tb)
        else:
            return crashhandler.crash_handler_lite(etype, evalue, tb)

    @observe('ipython_dir')
    def _ipython_dir_changed(self, change):
        old = change['old']
        new = change['new']
        if old is not Undefined:
            str_old = py3compat.cast_bytes_py2(os.path.abspath(old),
                sys.getfilesystemencoding()
            )
            if str_old in sys.path:
                sys.path.remove(str_old)
        str_path = py3compat.cast_bytes_py2(os.path.abspath(new),
            sys.getfilesystemencoding()
        )
        sys.path.append(str_path)
        ensure_dir_exists(new)
        readme = os.path.join(new, 'README')
        readme_src = os.path.join(get_ipython_package_dir(), u'config', u'profile', 'README')
        if not os.path.exists(readme) and os.path.exists(readme_src):
            shutil.copy(readme_src, readme)
        for d in ('extensions', 'nbextensions'):
            path = os.path.join(new, d)
            try:
                ensure_dir_exists(path)
            except OSError as e:
                # this will not be EEXIST
                self.log.error("couldn't create path %s: %s", path, e)
        self.log.debug("IPYTHONDIR set to: %s" % new)

    def load_config_file(self, suppress_errors=IPYTHON_SUPPRESS_ERRORS):
        """Load the config file.

        By default, errors in loading config are handled, and a warning
        printed on screen. For testing, the suppress_errors option is set
        to False, so errors will make tests fail.

        `supress_errors` default value is to be `None` in which case the
        behavior default to the one of `traitlets.Application`.

        The default value can be set :
           - to `False` by setting 'IPYTHON_SUPPRESS_ERRORS' environment variable to '0', or 'false' (case insensitive).
           - to `True` by setting 'IPYTHON_SUPPRESS_ERRORS' environment variable to '1' or 'true' (case insensitive).
           - to `None` by setting 'IPYTHON_SUPPRESS_ERRORS' environment variable to '' (empty string) or leaving it unset.

        Any other value are invalid, and will make IPython exit with a non-zero return code.
        """


        self.log.debug("Searching path %s for config files", self.config_file_paths)
        base_config = 'ipython_config.py'
        self.log.debug("Attempting to load config file: %s" %
                       base_config)
        try:
            if suppress_errors is not None:
                old_value = Application.raise_config_file_errors
                Application.raise_config_file_errors = not suppress_errors;
            Application.load_config_file(
                self,
                base_config,
                path=self.config_file_paths
            )
        except ConfigFileNotFound:
            # ignore errors loading parent
            self.log.debug("Config file %s not found", base_config)
            pass
        if suppress_errors is not None:
            Application.raise_config_file_errors = old_value
        
        for config_file_name in self.config_files:
            if not config_file_name or config_file_name == base_config:
                continue
            self.log.debug("Attempting to load config file: %s" %
                           self.config_file_name)
            try:
                Application.load_config_file(
                    self,
                    config_file_name,
                    path=self.config_file_paths
                )
            except ConfigFileNotFound:
                # Only warn if the default config file was NOT being used.
                if config_file_name in self.config_file_specified:
                    msg = self.log.warning
                else:
                    msg = self.log.debug
                msg("Config file not found, skipping: %s", config_file_name)
            except Exception:
                # For testing purposes.
                if not suppress_errors:
                    raise
                self.log.warning("Error loading config file: %s" %
                              self.config_file_name, exc_info=True)

    def init_profile_dir(self):
        """initialize the profile dir"""
        self._in_init_profile_dir = True
        if self.profile_dir is not None:
            # already ran
            return
        if 'ProfileDir.location' not in self.config:
            # location not specified, find by profile name
            try:
                p = ProfileDir.find_profile_dir_by_name(self.ipython_dir, self.profile, self.config)
            except ProfileDirError:
                # not found, maybe create it (always create default profile)
                if self.auto_create or self.profile == 'default':
                    try:
                        p = ProfileDir.create_profile_dir_by_name(self.ipython_dir, self.profile, self.config)
                    except ProfileDirError:
                        self.log.fatal("Could not create profile: %r"%self.profile)
                        self.exit(1)
                    else:
                        self.log.info("Created profile dir: %r"%p.location)
                else:
                    self.log.fatal("Profile %r not found."%self.profile)
                    self.exit(1)
            else:
                self.log.debug("Using existing profile dir: %r"%p.location)
        else:
            location = self.config.ProfileDir.location
            # location is fully specified
            try:
                p = ProfileDir.find_profile_dir(location, self.config)
            except ProfileDirError:
                # not found, maybe create it
                if self.auto_create:
                    try:
                        p = ProfileDir.create_profile_dir(location, self.config)
                    except ProfileDirError:
                        self.log.fatal("Could not create profile directory: %r"%location)
                        self.exit(1)
                    else:
                        self.log.debug("Creating new profile dir: %r"%location)
                else:
                    self.log.fatal("Profile directory %r not found."%location)
                    self.exit(1)
            else:
                self.log.info("Using existing profile dir: %r"%location)
            # if profile_dir is specified explicitly, set profile name
            dir_name = os.path.basename(p.location)
            if dir_name.startswith('profile_'):
                self.profile = dir_name[8:]

        self.profile_dir = p
        self.config_file_paths.append(p.location)
        self._in_init_profile_dir = False

    def init_config_files(self):
        """[optionally] copy default config files into profile dir."""
        self.config_file_paths.extend(SYSTEM_CONFIG_DIRS)
        # copy config files
        path = self.builtin_profile_dir
        if self.copy_config_files:
            src = self.profile

            cfg = self.config_file_name
            if path and os.path.exists(os.path.join(path, cfg)):
                self.log.warning("Staging %r from %s into %r [overwrite=%s]"%(
                        cfg, src, self.profile_dir.location, self.overwrite)
                )
                self.profile_dir.copy_config_file(cfg, path=path, overwrite=self.overwrite)
            else:
                self.stage_default_config_file()
        else:
            # Still stage *bundled* config files, but not generated ones
            # This is necessary for `ipython profile=sympy` to load the profile
            # on the first go
            files = glob.glob(os.path.join(path, '*.py'))
            for fullpath in files:
                cfg = os.path.basename(fullpath)
                if self.profile_dir.copy_config_file(cfg, path=path, overwrite=False):
                    # file was copied
                    self.log.warning("Staging bundled %s from %s into %r"%(
                            cfg, self.profile, self.profile_dir.location)
                    )


    def stage_default_config_file(self):
        """auto generate default config file, and stage it into the profile."""
        s = self.generate_config_file()
        fname = os.path.join(self.profile_dir.location, self.config_file_name)
        if self.overwrite or not os.path.exists(fname):
            self.log.warning("Generating default config file: %r"%(fname))
            with open(fname, 'w') as f:
                f.write(s)

    @catch_config_error
    def initialize(self, argv=None):
        # don't hook up crash handler before parsing command-line
        self.parse_command_line(argv)
        self.init_crash_handler()
        if self.subapp is not None:
            # stop here if subapp is taking over
            return
        cl_config = self.config
        self.init_profile_dir()
        self.init_config_files()
        self.load_config_file()
        # enforce cl-opts override configfile opts:
        self.update_config(cl_config)
