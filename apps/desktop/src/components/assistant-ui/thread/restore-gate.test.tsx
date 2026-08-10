// The restore affordance is only offered on turns a rewind can actually reach.
//
// The transcript on screen is the display projection — it carries lineage from
// before a compaction handoff, which the gateway can render but cannot
// truncate. The gateway marks the reachable turns with `rewind_id`; a bubble
// without one used to still show the button, and clicking it could only ever
// produce "target user message is no longer in session history".
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Thread } from '.'

const createdAt = new Date('2026-05-01T00:00:00.000Z')

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', TestResizeObserver)
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
  window.setTimeout(() => callback(performance.now()), 0)
)
vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
vi.stubGlobal('CSS', { escape: (str: string) => str })

Element.prototype.scrollTo = function scrollTo() {}

function userMessage(id: string, text: string, rewindId?: string): ThreadMessage {
  return {
    id,
    role: 'user',
    content: [{ type: 'text', text }],
    attachments: [],
    createdAt,
    metadata: { custom: { attachmentRefs: [], ...(rewindId ? { rewindId } : {}) } }
  } as ThreadMessage
}

function assistantMessage(id: string, text: string): ThreadMessage {
  return {
    id,
    role: 'assistant',
    content: [{ type: 'text', text }],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
  } as ThreadMessage
}

function Harness({ messages }: { messages: ThreadMessage[] }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages,
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread onRestoreToMessage={async () => {}} />
    </AssistantRuntimeProvider>
  )
}

const restoreButtons = () => screen.queryAllByRole('button', { name: 'Restore checkpoint' })

afterEach(() => {
  cleanup()
})

describe('restore is withdrawn', () => {
  // Restore is disabled until rewind identity is occurrence-bound. `rewind_id`
  // is ordinal + content digest, which an audit showed can name a different
  // turn than the bubble it rides on (session.undo divergence, ABA
  // replacement). These pin the withdrawal so it cannot be undone by accident;
  // the two gating cases they replace are kept below, skipped, ready to
  // re-enable with the feature.
  it('offers no restore button, even on a turn the gateway stamped', async () => {
    render(
      <Harness
        messages={[
          userMessage('u1', 'still in context', 'r1:0:deadbeefdeadbeef'),
          assistantMessage('a1', 'reply')
        ]}
      />
    )

    await screen.findByText('still in context')

    expect(restoreButtons()).toHaveLength(0)
  })

  it('offers no restore button when the gateway stamps nothing', async () => {
    render(<Harness messages={[userMessage('u1', 'first'), assistantMessage('a1', 'reply')]} />)

    await screen.findByText('first')

    expect(restoreButtons()).toHaveLength(0)
  })
})

describe.skip('restore button gating (re-enable with the feature)', () => {
  it('offers restore only on the turns the gateway stamped', async () => {
    render(
      <Harness
        messages={[
          // Pre-compaction lineage: on screen, but not in the model history.
          userMessage('u1', 'from before the compaction'),
          assistantMessage('a1', 'old reply'),
          userMessage('u2', 'still in context', 'r1:0:deadbeefdeadbeef'),
          assistantMessage('a2', 'new reply')
        ]}
      />
    )

    await screen.findByText('from before the compaction')

    // One stamped turn proves the gateway mints ids, so the unstamped bubble's
    // silence is meaningful — exactly one button, on the reachable turn.
    const buttons = restoreButtons()

    expect(buttons).toHaveLength(1)
    expect(buttons[0].closest('[data-slot="aui_user-bubble-actions"]')?.textContent).toContain('still in context')
  })

  it('keeps restore everywhere when the gateway stamps nothing', async () => {
    // An older gateway sends no ids at all. Gating on absence there would strip
    // restore from every message, so the positional path stays available.
    render(
      <Harness
        messages={[
          userMessage('u1', 'first'),
          assistantMessage('a1', 'reply one'),
          userMessage('u2', 'second'),
          assistantMessage('a2', 'reply two')
        ]}
      />
    )

    await screen.findByText('first')

    expect(restoreButtons()).toHaveLength(2)
  })
})
