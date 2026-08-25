import assert from 'node:assert/strict'

import { test } from 'vitest'

import { ensureMainWindow, revealFocusedWindow, revealMainWindow } from './main-window-lifecycle'

test('recreates a destroyed primary window without focusing it', () => {
  const destroyedWindow = {
    isDestroyed: () => true
  }

  let createCalls = 0
  let focusCalls = 0

  ensureMainWindow(destroyedWindow, {
    isReady: true,
    createWindow: () => {
      createCalls += 1
    },
    focusWindow: () => {
      focusCalls += 1
    }
  })

  assert.equal(createCalls, 1)
  assert.equal(focusCalls, 0)
})

test('waits for app readiness before recreating a primary window', () => {
  let createCalls = 0

  ensureMainWindow(null, {
    isReady: false,
    createWindow: () => {
      createCalls += 1
    },
    focusWindow: () => assert.fail('missing window must not be focused')
  })

  assert.equal(createCalls, 0)
})

test('focuses a live primary window for a normal second launch', () => {
  const liveWindow = {
    isDestroyed: () => false
  }

  let focusedWindow = null

  ensureMainWindow(liveWindow, {
    isReady: true,
    createWindow: () => assert.fail('live window must not be replaced'),
    focusWindow: window => {
      focusedWindow = window
    }
  })

  assert.equal(focusedWindow, liveWindow)
})

test('leaves live-window focus to deep-link delivery', () => {
  const liveWindow = {
    isDestroyed: () => false
  }

  ensureMainWindow(liveWindow, {
    isReady: true,
    createWindow: () => assert.fail('live window must not be replaced'),
    focusWindow: () => assert.fail('deep-link delivery owns focus'),
    focusExisting: false
  })
})

test('reveals Playwright windows without stealing foreground focus', () => {
  const actions: string[] = []

  revealMainWindow(
    {
      show: () => actions.push('show'),
      showInactive: () => actions.push('showInactive')
    },
    true
  )

  assert.deepEqual(actions, ['showInactive'])
})

test('reveals normal windows with the production focus behavior', () => {
  const actions: string[] = []

  revealMainWindow(
    {
      show: () => actions.push('show'),
      showInactive: () => actions.push('showInactive')
    },
    false
  )

  assert.deepEqual(actions, ['show'])
})

test('reveals Playwright auxiliary windows without focusing them', () => {
  const actions: string[] = []

  revealFocusedWindow(
    {
      focus: () => actions.push('focus'),
      show: () => actions.push('show'),
      showInactive: () => actions.push('showInactive')
    },
    true
  )

  assert.deepEqual(actions, ['showInactive'])
})

test('reveals and focuses normal auxiliary windows', () => {
  const actions: string[] = []

  revealFocusedWindow(
    {
      focus: () => actions.push('focus'),
      show: () => actions.push('show'),
      showInactive: () => actions.push('showInactive')
    },
    false
  )

  assert.deepEqual(actions, ['show', 'focus'])
})
