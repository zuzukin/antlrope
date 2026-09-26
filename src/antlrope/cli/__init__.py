# Copyright 2026 Christopher Barber
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The `antlrope` command-line interface.

`antlrope` is a command group. The entry point lives in
[main][antlrope.cli.main], and each subcommand is its own module that registers
itself there (`gen`, `regen`, `up-to-date`, `check`, `rules`, and `tokens`).
"""
