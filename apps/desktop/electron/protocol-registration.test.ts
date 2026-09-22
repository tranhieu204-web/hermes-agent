import assert from 'node:assert/strict'
import { test } from 'vitest'

import { installationOwnsProtocolRegistration } from './protocol-registration'

test('the exact managed-launcher marker suppresses Electron ownership', () => {
  assert.equal(installationOwnsProtocolRegistration('1'), false)
  assert.equal(installationOwnsProtocolRegistration(undefined), true)
  assert.equal(installationOwnsProtocolRegistration('0'), true)
})
