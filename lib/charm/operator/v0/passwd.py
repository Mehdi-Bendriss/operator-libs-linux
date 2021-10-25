# Copyright 2021 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Representations of the system's users and groups, and abstractions around managing them.

The `passwd` module provides convenience methods and abstractions around users and
groups on a Linux system, in order to make adding and managing users and groups easy.
In the `passwd` module, :class:`Passwd` creates dictionaries of :class:`User` and
:class:`Group` objects accessible by plain :str: keys, and exposed as properties on
`Passwd.groups` and `Passwd.users`.

Users and groups are fully populated, referencing the object types of both. A :class:`User`
object has a `groups` property which references :class:`Group` objects, and a :class:`Group`
object has a `users` property which references :class:`User` objects. In order to make
using this easier, :class:`Passwd` is provided which handled the initialization of both.

Typical usage:
  import passwd
  try:
      passwd.add_user(
          "test",
          group=Group("test", gid=1001),
      )
  except UserError as e:
      logger.error(e.message)

  ##############################

  try:
      snap_user = passwd.Passwd().users["snap"]
      snap_user.ensure(passwd.UserState.NoLogin)
  except UserNotFoundError:
      logger.error("User snap not found!")
"""

import os
import logging
import re
import subprocess

from collections import UserDict
from enum import Enum
from subprocess import CalledProcessError
from typing import List, Optional, Union


logger = logging.getLogger(__name__)


class Error(Exception):
    """Base class of most errors raised by this library."""

    def __repr__(self):
        return f"<{type(self).__module__}.{type(self).__name__} {self.args}>"

    @property
    def name(self):
        """Return a string representation of the model plus class."""
        return f"<{type(self).__module__}.{type(self).__name__}>"

    @property
    def message(self):
        """Return the message passed as an argument."""
        return self.args[0]


class UserState(Enum):
    """The state of a user on the system or in the cache."""

    Present = "present"
    Absent = "absent"
    Disabled = "disabled"
    NoLogin = "nologin"


class UserError(Error):
    """Raised when there's an error managing a user account."""


class UserNotFoundError(Error):
    """Raised when a requested user is not known to the system."""


class GroupError(Error):
    """Raised when there's an error managing a user account."""


class GroupNotFoundError(Error):
    """Raised when a requested group is not known to the system."""


class User(object):
    """Represents a user and its properties.

    :class:`User` exposes the following properties about a user:
      - name: the username of a user
      - uid: an `int` representing a useruid
      - gid: an `int` representing a group
      - group: a :class:`Group` representing a user's group
      - homedir: a user's homedir
      - shell: a user's shell
      - state: a :class:`UserState` represnding a user's state
      - gecos: an (Optional) comment describing user information
    """

    def __init__(
        self,
        name,
        state: "UserState",
        group: Optional[Union[int, "Group"]] = None,
        homedir: Optional[str] = "",
        shell: Optional[str] = "",
        uid: Optional[int] = None,
        gecos: Optional[str] = "",
        groups: Optional[List[Union["str", "Group"]]] = None,
    ) -> None:
        self._name = name
        self._uid = uid
        self._homedir = homedir
        self._shell = shell
        self._state = state
        self._gecos = gecos

        if group:
            self._primary_group = group if type(group) is Group else Passwd.group_by_gid(group)
        else:
            self._primary_group = None
        self._groups = (
            [g if type(g) is Group else Passwd.group_by_gid(g) for g in groups] if groups else []
        )

    def __hash__(self):
        """A basic hash so this class can be used in Mappings and dicts."""
        return hash((self._name, self._uid))

    def __repr__(self):
        """A representation of the user."""
        return f"<{self.__module__}.{self.__class__.__name__}: {self.__dict__}>"

    def __str__(self):
        """A human-readable representation of the user."""
        return "<{}: {}-{}.{}: {} -- {}>".format(
            self.__class__.__name__,
            self._name,
            self._uid,
            self._homedir,
            self.groups,
            str(self._state),
        )

    @property
    def name(self) -> str:
        """Returns the name of the user."""
        return self._name

    @property
    def uid(self) -> int:
        """Returns the ID of a user."""
        return self._uid

    @property
    def homedir(self) -> str:
        """Returns the homedir of a user."""
        return self._homedir

    @property
    def shell(self) -> str:
        """Returns the shell for a user."""
        return self._shell

    @property
    def gecos(self) -> str:
        """Returns the GECOS for a user."""
        return self._gecos

    @property
    def primary_group(self) -> "Group":
        """Returns the primary group of a user."""
        return self._primary_group

    @property
    def groups(self) -> List["Group"]:
        """Returns the groups for this user."""
        return self._groups

    def ensure_state(
        self,
        state: UserState,
    ):
        """Ensures that a user is in a given state.

        Args:
          state: a :class:`UserState` to reconcile to.

        Raises:
          UserError if an error is encountered
        """
        if state is UserState.NoLogin:
            self._enable_account() if self.state == UserState.Disabled else self._disable_login()
        elif state is UserState.Disabled:
            self._disable_account()
        elif state is UserState.Present:
            self._enable_account() if self.state == UserState.Disabled else self._add()
        self._state = state

    def _add(self) -> None:
        """Add a user to to the system."""
        try:
            if self.present:
                return
        except UserNotFoundError as e:
            logger.debug("User {} not found, adding", self.name)

        argbuilder = lambda x, y: [x, y] if y else []

        try:
            args = []
            params = [
                ["-g", self.primary_group.gid if self.primary_group else None],
                ["-s", self.shell],
                ["-d", self.homedir],
                ["-u", self.uid],
                ["-c", self.gecos],
            ]

            for p in params:
                args.extend(argbuilder(p[0], p[1]))

            if self.uid and self.uid < 1000:
                args.append("-r")

            subprocess.check_call(["useradd", *args, self.name])
        except CalledProcessError as e:
            raise UserError(f"Could not add user '{self.name}' to the system: {e.output}")

    def _remove(self) -> None:
        """Removes a user from the system."""
        if not self.present:
            return

        try:
            subprocess.check_call(["userdel", self.name])
        except CalledProcessError as e:
            raise UserError(f"Could not remove user '{self.name}' to the system: {e.output}")

    def _disable_login(self):
        """Disable logins for a user by setting the shell to `/sbin/nologin."""
        if not self._check_if_present(add_if_absent=True):
            raise UserError(
                f"Could not disable login for user account {self.name}. User is not present!"
            )
        try:
            subprocess.check_call(["usermod", "-s", "/sbin/nologin", self.name])
        except CalledProcessError as e:
            raise UserError(f"Could not disable login for user account {self.name}: {e.output}")

    def _disable_account(self):
        """Disable a user account by locking it."""
        if not self._check_if_present(add_if_absent=True):
            raise UserError(f"Could not disable account {self.name}. User is not present!")
        try:
            subprocess.check_call(["usermod", "-L", self.name])
        except CalledProcessError as e:
            raise UserError(f"Could not disable user account {self.name}: {e.output}")

    def _enable_account(self):
        """Enable a user account by unlocking it."""
        try:
            subprocess.check_call(["usermod", "-U", self.name])
        except CalledProcessError as e:
            raise UserError(f"Could not enable user account {self.name}: {e.output}")

    def _check_if_present(self, add_if_absent: Optional[bool] = False) -> bool:
        """Ensures a user is present in /etc/passwd.

        Args:
            add_if_absent: an (Optional) boolean for whether the user should be added if not found. Default `false`.
        """
        matcher = (
            rf"{self.name}:{'!' if self.state is UserState.Disabled else 'x'}:{self.uid}:"
            + f"{self.primary_group.gid}:{self.gecos}:{self.homedir}:{self.shell}"
        )
        found = False

        with open("/etc/passwd", "r") as f:
            for line in f:
                if re.match(matcher, line.strip()):
                    found = True
                    break

        if not found and add_if_absent:
            self._add()
            return True

        return found

    @property
    def present(self) -> bool:
        """Returns whether or not a user is present."""
        return self._check_if_present()

    @property
    def state(self) -> UserState:
        """Returns the current state of a user."""
        return self._state

    @state.setter
    def state(self, state: UserState) -> None:
        """Sets the user state to a given value.

        Args:
          state: a :class:`UserState` to reconcile the user to.

        Raises:
          UserError if an error is encountered
        """
        if self._state is not state:
            self.ensure(state)
        self._state = state


class Group(object):
    """Represents a group and its properties.

    :class:`Group` exposes the following properties about a group:
        - name: the username of a user
        - gid: an `int` representing a group
        - users: a list of user IDs belonging to the group
    """

    def __init__(
        self, name: str, users: Union[List[str], List[User]], gid: Optional[Union[str, int]] = None
    ):
        self._name = name
        self._gid = (gid) if gid else None
        self._users = [user.name if type(user) == User else user for user in users]

    def __str__(self) -> str:
        """A human-readable representation of the group."""
        return f"<{self.__class__.__name__}: {self._name}-{self._gid} -- {self._users}>"

    def __eq__(self, other):
        return (self._name, self._gid) == (other.name, other.gid)

    @property
    def name(self) -> str:
        """Returns the name of the group."""
        return self._name

    @property
    def gid(self) -> int:
        """Returns the ID of the group."""
        return self._gid

    @property
    def users(self) -> List[User]:
        """Returns a list of users in the group."""
        return self._users

    def add(self) -> None:
        """Adds a group to the system.

        Raises:
            CalledProcessError
        """
        try:
            subprocess.check_call(["groupadd", "-g", self.gid, self.name])
        except CalledProcessError as e:
            raise GroupError(f"Could not add group {self.name}! Reason: {e.output}")

    def remove(self) -> None:
        """Removes a group from the system.

        Raises:
            CalledProcessError
        """
        try:
            subprocess.check_call(["groupdel", self.name])
        except CalledProcessError as e:
            raise GroupError(f"Could not delete group {self.name}! Reason: {e.output}")


class Users(UserDict):
    """A very small wrapper so __getitem__ returns nice errors."""

    def __getitem__(self, key: str) -> User:
        """Return a :class:`UserNotFoundError` if it isn't there."""
        try:
            return super().__getitem__(key)
        except KeyError:
            raise UserNotFoundError(f"User '{key}' not found!")


class Groups(UserDict):
    """A very small wrapper so __getitem__ returns nice errors."""

    def __getitem__(self, key: str) -> Group:
        """Return a :class:`GroupNotFoundError` if it isn't there."""
        try:
            return super().__getitem__(key)
        except KeyError:
            raise GroupNotFoundError(f"Group '{key}' not found!")


class Passwd:
    """An abstraction to represent users and groups present on the system.

    When instantiated, :class:`Passwd` parses out /etc/group and /etc/passwd
    to create abstracted objects.
    """

    # Leave these ass class-level so we can hit them with @classmethod
    _groups = Groups()
    _users = Users()

    def __init__(self):
        self._load_groups()
        self._load_users()
        self._realize_users()

    @property
    def users(self) -> Users:
        """Return a mapping of the users on the system."""
        return self._users

    @property
    def groups(self) -> Groups:
        """Return a mapping of the groups on the system."""
        return self._groups

    def _load_users(self) -> None:
        """Parse /etc/passwd to get information about available passwd."""
        if not os.path.isfile("/etc/passwd"):
            raise UserError("/etc/passwd not found on the system!")

        with open("/etc/passwd", "r") as f:
            for line in f:
                if line.strip():
                    user = self._parse_passwd_line(line)
                    self._users[user.name] = user

    def _parse_passwd_line(self, line) -> User:
        """Get values out of /etc/passwd and turn them into a :class:`User` object to cache."""
        fields = line.split(":")
        name = fields[0]
        uid = int(fields[2])
        gid = int(fields[3])
        gecos = fields[4]
        homedir = fields[5]
        shell = fields[6].strip()

        state = UserState.NoLogin if shell == "/usr/sbin/nologin" else UserState.Present
        return User(name, state, homedir=homedir, shell=shell, uid=uid, group=gid, gecos=gecos)

    @classmethod
    def group_by_gid(cls, gid: int) -> Group:
        """Look up a group by group id.
        Args:
            gid: an `int` representing the groupid
        Raises:
            GroupNotFoundError
        """
        # Make sure we know about groups
        cls._fetch_groups_for_user()

        for group in cls._groups.values():
            if group.gid == gid:
                return group

        raise GroupNotFoundError(f"Could not find a group with GID {gid}!")

    @classmethod
    def _fetch_groups_for_user(cls) -> Groups:
        """Retrieve or parse out groups for single-user initialization."""
        if not cls._groups:
            cls._load_groups()

        return cls._groups

    @classmethod
    def _load_groups(cls) -> None:
        """Parse /etc/group to get information about available groups."""

        if not os.path.isfile("/etc/group"):
            raise GroupError("/etc/group not found on the system!")

        with open("/etc/group", "r") as f:
            for line in f:
                if line.strip():
                    group = cls._parse_groups_line(line)
                    cls._groups[group.name] = group

    @classmethod
    def _parse_groups_line(cls, line) -> Group:
        """Get values out of /etc/group and turn them into a :class:`Group` object to cache."""
        fields = line.split(":")
        name = fields[0]
        gid = int(fields[2])
        usernames = [u for u in fields[3].strip().split(",") if u]
        return Group(name, usernames, gid=gid)

    def _realize_users(self) -> None:
        """Map user strings to :class:`User` objects for coherency."""
        for k, v in self._groups.items():
            v._users = [
                self._users[uname] if type(uname) is not User else uname for uname in v.users
            ]
            self._groups[k] = v

    def add_group(self, group: Group) -> None:
        """Adds a group to the system.

        Args:
            group: a :class:`Group` object to add

        Raises:
            CalledProcessError
        """
        try:
            args = ["-g", group.gid] if group.gid else []
            subprocess.check_call(["groupadd", *args, group.name])
        except CalledProcessError as e:
            raise GroupError(f"Could not add group {self.name}! Reason: {e.output}")

    def add_user(self, user: User) -> None:
        """Adds a user to the system.

        Args:
            user: a :class:`User` object to add
        """
        user.ensure(state=UserState.Present)