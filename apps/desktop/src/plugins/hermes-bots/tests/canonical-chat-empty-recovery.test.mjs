import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadCanonicalRecovery({ openSession, request }) {
  const start = source.indexOf('const canonicalCreations = new Map()')
  const end = source.indexOf('function displayName(', start)
  const saved = []
  const context = {
    host: { openSession, request },
    saveBotMeta: (name, patch) => saved.push({ name, patch }),
    $hideBotChats: { get: () => false },
    window: { setTimeout: callback => callback() }
  }
  const section = source
    .slice(start, end)
    .concat('\nglobalThis.__canonical = { openBotCanonicalChat };\n')

  assert.notEqual(start, -1, 'canonical chat section is missing')
  assert.notEqual(end, -1, 'canonical chat section delimiter is missing')
  vm.runInNewContext(section, context, { filename: 'canonical-recovery.js' })
  return { ...context.__canonical, saved }
}

test('regression: an empty session list clears a stale pin and creates a replacement', async () => {
  const opened = []
  const runtime = loadCanonicalRecovery({
    openSession: async id => opened.push(id),
    request: async method => {
      if (method === 'session.list') return { sessions: [] }
      if (method === 'session.create') return { stored_session_id: 'replacement', session_id: 'replacement-runtime' }
      return {}
    }
  })

  assert.equal(await runtime.openBotCanonicalChat('ops', 'stale-pin'), 'replacement')
  assert.deepEqual(opened, ['replacement'])
  assert.deepEqual(JSON.parse(JSON.stringify(runtime.saved)), [
    { name: 'ops', patch: { chat: null } },
    { name: 'ops', patch: { chat: 'replacement' } }
  ])
})

test('regression: a corrupted visible-session pin repairs to the hidden Bot Chat', async () => {
  const opened = []
  const runtime = loadCanonicalRecovery({
    openSession: async id => opened.push(id),
    request: async method => {
      if (method === 'session.list') {
        return {
          sessions: [
            { id: 'wrong-visible-session', title: 'Unrelated chat' },
            { id: 'canonical', title: 'Bot Chat' }
          ]
        }
      }
      return {}
    }
  })

  assert.equal(await runtime.openBotCanonicalChat('ops', 'wrong-visible-session'), 'canonical')
  assert.deepEqual(opened, ['canonical'])
  assert.deepEqual(JSON.parse(JSON.stringify(runtime.saved)), [
    { name: 'ops', patch: { chat: 'canonical' } }
  ])
})
