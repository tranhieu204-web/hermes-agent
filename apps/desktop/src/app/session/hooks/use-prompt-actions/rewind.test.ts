import { describe, expect, it } from 'vitest'

import { textPart } from '@/lib/chat-messages'

import { planEdit, planReload, planRestore, truncateSubmitParams } from './rewind'

const userMessage = (id: string, text: string, rewindId?: string) => ({
  id,
  role: 'user' as const,
  parts: [textPart(text)],
  ...(rewindId ? { rewindId } : {})
})

const assistantMessage = (id: string, text: string) => ({
  id,
  role: 'assistant' as const,
  parts: [textPart(text)]
})

describe('truncateSubmitParams', () => {
  it('omits truncation fields when there is no target', () => {
    expect(truncateSubmitParams(undefined)).toEqual({})
    expect(truncateSubmitParams({})).toEqual({})
  })

  it('sends the rewind id alone, never a blanket wipe confirmation', () => {
    // Emptying a transcript must be an explicit act. Sending
    // confirm_empty_truncate alongside every id-addressed rewind is what let a
    // misattached id delete a session.
    expect(truncateSubmitParams({ messageId: 'r2:3:abc', ordinal: 7 })).toEqual({
      truncate_before_message_id: 'r2:3:abc'
    })
  })

  it('sends the dedicated wipe confirmation only when the cut empties the transcript', () => {
    // The generic confirm is deliberately never sent on the id path — a blanket
    // one is what let a misattached id delete a session. This flag is separate,
    // and only rides along when the user acted on the opening turn.
    expect(truncateSubmitParams({ messageId: 'r2:0:abc', wipesTranscript: true })).toEqual({
      truncate_before_message_id: 'r2:0:abc',
      confirm_delete_entire_transcript: true
    })
    expect(truncateSubmitParams({ messageId: 'r2:3:abc', wipesTranscript: false })).toEqual({
      truncate_before_message_id: 'r2:3:abc'
    })
  })

  it('falls back to the ordinal only when no id is available', () => {
    expect(truncateSubmitParams({ ordinal: 1 })).toEqual({
      truncate_before_user_ordinal: 1
    })
    expect(truncateSubmitParams({ ordinal: 0 })).toEqual({
      truncate_before_user_ordinal: 0,
      confirm_empty_truncate: true
    })
  })
})

describe('rewind planners carry the gateway identity', () => {
  const messages = [
    userMessage('u1', 'first', 'r1:0:aaa'),
    assistantMessage('a1', 'reply one'),
    userMessage('u2', 'second', 'r1:1:bbb'),
    assistantMessage('a2', 'reply two')
  ]

  it('planRestore prefers the live message id over the captured target', () => {
    // The click captured a stale id; state is authoritative.
    const plan = planRestore(messages, 'u2', { rewindId: 'r1:9:stale', text: 'second', userOrdinal: 1 })

    expect(plan.truncateMessageId).toBe('r1:1:bbb')
    expect(plan.sourceIndex).toBe(2)
  })

  it('planRestore leaves the id undefined when the turn is not rewindable', () => {
    const unstamped = [userMessage('u1', 'ancient'), assistantMessage('a1', 'reply')]
    const plan = planRestore(unstamped, 'u1', { rewindId: null, text: 'ancient', userOrdinal: 0 })

    // No id anywhere — the ordinal fallback is all that is left, which is
    // exactly the legacy-gateway path.
    expect(plan.truncateMessageId).toBeUndefined()
    expect(plan.truncateOrdinal).toBe(0)
  })

  it('planReload targets the user turn behind the assistant message', () => {
    expect(planReload(messages, 'a2')?.truncateMessageId).toBe('r1:1:bbb')
  })

  it('planEdit carries the id, but never for a failed turn', () => {
    const edited = {
      role: 'user' as const,
      sourceId: 'u2',
      parentId: 'u2',
      content: [{ type: 'text' as const, text: 'second, revised' }]
    }

    expect(planEdit(messages, edited as never)?.truncateMessageId).toBe('r1:1:bbb')

    // A turn whose assistant reply errored never reached the gateway, so there
    // is nothing to truncate — it resubmits plainly.
    const failed = [
      userMessage('u1', 'first', 'r1:0:aaa'),
      { ...assistantMessage('a1', ''), error: 'boom' }
    ]

    const failedPlan = planEdit(failed, {
      role: 'user' as const,
      sourceId: 'u1',
      parentId: 'u1',
      content: [{ type: 'text' as const, text: 'first, revised' }]
    } as never)

    expect(failedPlan?.isFailedTurn).toBe(true)
    expect(failedPlan?.truncateMessageId).toBeUndefined()
    expect(failedPlan?.truncateOrdinal).toBeUndefined()
  })
})

describe('the opening turn is marked as a transcript wipe', () => {
  const opening = [
    userMessage('u1', 'first', 'r2:0:aaa'),
    assistantMessage('a1', 'reply one'),
    userMessage('u2', 'second', 'r2:1:bbb'),
    assistantMessage('a2', 'reply two')
  ]

  it('planReload marks it for the first turn only', () => {
    expect(planReload(opening, 'a1')?.wipesTranscript).toBe(true)
    expect(planReload(opening, 'a2')?.wipesTranscript).toBe(false)
  })

  it('planEdit marks it for the first turn only', () => {
    const edit = (sourceId: string, text: string) =>
      planEdit(opening, { role: 'user' as const, sourceId, parentId: sourceId, content: [{ type: 'text' as const, text }] } as never)

    expect(edit('u1', 'first, revised')?.wipesTranscript).toBe(true)
    expect(edit('u2', 'second, revised')?.wipesTranscript).toBe(false)
  })

  it('planRestore takes it from the clicked bubble, not from position', () => {
    expect(planRestore(opening, 'u1', { rewindId: 'r2:0:aaa', wipesTranscript: true }).wipesTranscript).toBe(true)
    expect(planRestore(opening, 'u2', { rewindId: 'r2:1:bbb', wipesTranscript: false }).wipesTranscript).toBe(false)
  })
})
