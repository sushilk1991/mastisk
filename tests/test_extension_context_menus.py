from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest


def test_context_menu_setup_coalesces_overlapping_lifecycle_events() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the Chrome service-worker regression harness")

    harness = textwrap.dedent(
        r"""
        const fs = require('fs');
        const vm = require('vm');

        const errors = [];
        const items = new Set();
        let pendingRemovals = [];
        let removalBatchScheduled = false;
        const event = { addListener() {}, removeListener() {} };
        const runtime = {
          onInstalled: event,
          onStartup: event,
          onMessage: event,
          id: 'mastisk-test',
          lastError: null,
        };
        const contextMenus = {
          onClicked: event,
          removeAll() {
            return new Promise((resolve) => {
              pendingRemovals.push(resolve);
              if (removalBatchScheduled) return;
              removalBatchScheduled = true;
              setTimeout(() => {
                items.clear();
                const resolvers = pendingRemovals;
                pendingRemovals = [];
                removalBatchScheduled = false;
                for (const done of resolvers) done();
              }, 0);
            });
          },
          create(properties, callback) {
            if (items.has(properties.id)) {
              const message = `Cannot create item with duplicate id ${properties.id}`;
              errors.push(message);
              runtime.lastError = { message };
            } else {
              items.add(properties.id);
              runtime.lastError = null;
            }
            if (callback) {
              queueMicrotask(() => {
                callback();
                runtime.lastError = null;
              });
            }
            return properties.id;
          },
        };
        const context = vm.createContext({
          chrome: {
            runtime,
            contextMenus,
            notifications: { onClicked: event },
            alarms: { onAlarm: event },
          },
          console,
          setTimeout,
          clearTimeout,
          queueMicrotask,
        });
        vm.runInContext(fs.readFileSync('extension/background.js', 'utf8'), context);
        Promise.all([context.setupContextMenus(), context.setupContextMenus()])
          .then(() => console.log(JSON.stringify({ items: [...items], errors })))
          .catch((error) => {
            console.error(error);
            process.exit(2);
          });
        """
    )
    result = subprocess.run(
        [node],
        input=harness,
        cwd=".",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["errors"] == []
    assert set(observed["items"]) == {
        "mastisk-page",
        "mastisk-selection",
        "mastisk-link",
    }
