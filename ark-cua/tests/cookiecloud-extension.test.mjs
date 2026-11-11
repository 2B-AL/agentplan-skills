// Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import test from 'node:test';
import { mergeSettings } from '../scripts/cookiecloud-extension.mjs';

test('settings remain a JSON string, preserve other options, and do not mutate the original', () => {
  const oldValue = randomBytes(16).toString('hex'), newValue = randomBytes(16).toString('hex');
  const old = JSON.stringify({ domains: 'jd.com', password: oldValue, keep_live: '', blacklist: 'unrelated.test', expire_minutes: 60, extra_option: true });
  const plan = mergeSettings(old, { domains: 'jd.com', password: newValue, type: 'up' }, false, false);
  const value = JSON.parse(plan.raw);
  assert.equal(value.password, newValue); assert.equal(value.extra_option, true);
  assert.equal(value.blacklist, 'unrelated.test'); assert.equal(value.expire_minutes, 60);
  assert.equal(JSON.parse(old).password, oldValue);
});

test('requested websites join the local list without confirmation for the same connection', () => {
  const privateValue = randomBytes(16).toString('hex');
  const connection = { endpoint: 'https://receiver.test/desk-a', uuid: 'connection-a' };
  const raw = JSON.stringify({ ...connection, domains: 'xiaohongshu.com\nunlisted-keyword', password: privateValue });
  const plan = mergeSettings(raw, { ...connection, domains: 'jd.com' }, false, false);
  assert.equal(plan.confirmation_required, undefined);
  assert.deepEqual(plan.domains, ['jd.com', 'unlisted-keyword', 'xiaohongshu.com']);
  assert.equal(JSON.parse(plan.raw).domains, 'jd.com\nunlisted-keyword\nxiaohongshu.com');
  assert.deepEqual(mergeSettings(undefined, { ...connection, domains: 'jd.com' }, false, false).domains, ['jd.com']);
  // Turning page storage off is reported, never silent.
  for (const [before, after, reported] of [[1, 0, true], ['1', 0, true], [0, 0, false], [1, 1, false], [undefined, 0, false]]) {
    const previous = JSON.stringify({ ...connection, domains: 'jd.com', with_storage: before });
    assert.equal(mergeSettings(previous, { ...connection, domains: 'jd.com', with_storage: after }, false, false).local_storage_turned_off, reported);
  }
});

test('moving from another desktop or narrowing all websites needs the matching confirmation', () => {
  const privateValue = randomBytes(16).toString('hex');
  const desktopB = { endpoint: 'https://receiver.test/desk-b', uuid: 'connection-b', domains: 'jd.com' };
  const cases = [
    [{ endpoint: 'https://receiver.test/desk-a', uuid: 'connection-a', domains: 'xiaohongshu.com' }, { other_desktop_configured: true, local_scope_all_websites: false }],
    [{ endpoint: desktopB.endpoint, uuid: desktopB.uuid, domains: '' }, { other_desktop_configured: false, local_scope_all_websites: true }],
    [{ endpoint: 'https://receiver.test/desk-a', uuid: 'connection-a', domains: '' }, { other_desktop_configured: true, local_scope_all_websites: true }],
  ];
  for (const [previous, flags] of cases) {
    const raw = JSON.stringify({ ...previous, password: privateValue });
    const plan = mergeSettings(raw, desktopB, false, false);
    assert.equal(plan.confirmation_required, true); assert.equal(plan.raw, undefined);
    assert.equal(plan.other_desktop_configured, flags.other_desktop_configured);
    assert.equal(plan.local_scope_all_websites, flags.local_scope_all_websites);
    assert.ok(!JSON.stringify(plan).includes(privateValue));
    const confirmed = mergeSettings(raw, desktopB, flags.local_scope_all_websites, flags.other_desktop_configured);
    // A new desktop receives only the requested websites, not the previous desktop's list.
    assert.deepEqual(confirmed.domains, ['jd.com']); assert.equal(JSON.parse(confirmed.raw).uuid, 'connection-b');
  }
});

test('malformed settings or an empty selected scope cannot be written', () => {
  for (const raw of ['not JSON', '[]', 'null', { domains: 'jd.com' }]) assert.throws(() => mergeSettings(raw, { domains: 'jd.com' }, false));
  assert.throws(() => mergeSettings(undefined, { domains: '' }, false));
  assert.equal(JSON.parse(mergeSettings(undefined, { domains: 'jd.com', type: 'up' }, false).raw).type, 'up');
});
