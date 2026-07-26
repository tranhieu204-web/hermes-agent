import { AssistantRuntimeProvider, type ThreadMessage, useAuiState } from '@assistant-ui/react'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import { textPart } from '@/lib/chat-messages'
import { useIncrementalExternalStoreRuntime } from '@/lib/incremental-external-store-runtime'

import { useRuntimeMessageRepository } from './runtime-repository'

function message(id: string, role: ChatMessage['role'], text: string): ChatMessage {
  return { id, role, parts: [textPart(text)] }
}

function RuntimeMessages() {
  const messages = useAuiState(state => state.thread.messages)

  return (
    <ol>
      {messages.map((item, index) => (
        <li data-message-id={item.id} data-testid="runtime-message" key={index}>
          {item.content.find(part => part.type === 'text')?.text}
        </li>
      ))}
    </ol>
  )
}

function RuntimeHarness({ messages, scopeKey = 'lineage-a' }: { messages: ChatMessage[]; scopeKey?: string }) {
  const messageRepository = useRuntimeMessageRepository(messages, scopeKey)

  const runtime = useIncrementalExternalStoreRuntime<ThreadMessage>({
    messageRepository,
    isRunning: false,
    setMessages: () => {},
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RuntimeMessages />
    </AssistantRuntimeProvider>
  )
}

function renderedMessages() {
  return screen.getAllByTestId('runtime-message').map(element => ({
    id: element.getAttribute('data-message-id'),
    text: element.textContent
  }))
}

describe('runtime message repository', () => {
  it('leaves unique message ids unchanged', () => {
    const unique = [message('user-id', 'user', 'question'), message('assistant-id', 'assistant', 'answer')]

    render(<RuntimeHarness messages={unique} />)

    expect(renderedMessages().map(item => item.id)).toEqual(['user-id', 'assistant-id'])
    expect(unique.map(item => item.id)).toEqual(['user-id', 'assistant-id'])
  })

  it('keeps prior renderer ids stable when an appended source id collides with a generated suffix', async () => {
    const initialSource = [message('x', 'user', 'one'), message('x', 'assistant', 'two')]
    const { rerender } = render(<RuntimeHarness messages={initialSource} />)
    const initialRendered = renderedMessages()

    expect(initialRendered).toEqual([
      { id: 'x', text: 'one' },
      { id: 'x:renderer-duplicate:2', text: 'two' }
    ])

    const appendedSource = [
      ...initialSource.map(item => ({ ...item })),
      message('x:renderer-duplicate:2', 'user', 'three')
    ]

    rerender(<RuntimeHarness messages={appendedSource} />)

    await waitFor(() => expect(renderedMessages().map(item => item.text)).toEqual(['one', 'two', 'three']))

    const appendedRendered = renderedMessages()
    expect(appendedRendered.slice(0, 2)).toEqual(initialRendered)
    expect(new Set(appendedRendered.map(item => item.id)).size).toBe(3)
    expect(initialSource.map(item => item.id)).toEqual(['x', 'x'])
    expect(appendedSource.map(item => item.id)).toEqual(['x', 'x', 'x:renderer-duplicate:2'])
  })

  it('reserves historical renderer ids while their source occurrence is temporarily absent', async () => {
    const initialSource = [message('x', 'user', 'one'), message('x', 'assistant', 'two')]
    const { rerender } = render(<RuntimeHarness messages={initialSource} />)

    await waitFor(() =>
      expect(renderedMessages()).toEqual([
        { id: 'x', text: 'one' },
        { id: 'x:renderer-duplicate:2', text: 'two' }
      ])
    )

    const withoutDuplicate = [
      { ...initialSource[0] },
      message('x:renderer-duplicate:2', 'assistant', 'legitimate suffix-shaped id')
    ]

    rerender(<RuntimeHarness messages={withoutDuplicate} />)

    await waitFor(() =>
      expect(renderedMessages()).toEqual([
        { id: 'x', text: 'one' },
        {
          id: 'x:renderer-duplicate:2:renderer-duplicate:2',
          text: 'legitimate suffix-shaped id'
        }
      ])
    )

    const restoredDuplicate = [
      ...initialSource.map(item => ({ ...item })),
      { ...withoutDuplicate[1] }
    ]

    rerender(<RuntimeHarness messages={restoredDuplicate} />)

    await waitFor(() =>
      expect(renderedMessages()).toEqual([
        { id: 'x', text: 'one' },
        { id: 'x:renderer-duplicate:2', text: 'two' },
        {
          id: 'x:renderer-duplicate:2:renderer-duplicate:2',
          text: 'legitimate suffix-shaped id'
        }
      ])
    )
    expect(new Set(renderedMessages().map(item => item.id)).size).toBe(3)
    expect(initialSource.map(item => item.id)).toEqual(['x', 'x'])
    expect(withoutDuplicate.map(item => item.id)).toEqual(['x', 'x:renderer-duplicate:2'])
    expect(restoredDuplicate.map(item => item.id)).toEqual(['x', 'x', 'x:renderer-duplicate:2'])
  })

  it('resets historical renderer-id reservations only when the transcript lineage changes', async () => {
    const transcriptA = [message('a', 'user', 'one'), message('a', 'assistant', 'two')]
    const { rerender } = render(<RuntimeHarness messages={transcriptA} scopeKey="lineage-a" />)

    await waitFor(() =>
      expect(renderedMessages()).toEqual([
        { id: 'a', text: 'one' },
        { id: 'a:renderer-duplicate:2', text: 'two' }
      ])
    )

    const sameLineageWithoutDuplicate = [
      { ...transcriptA[0] },
      message('a:renderer-duplicate:2', 'assistant', 'same-lineage legitimate id')
    ]

    rerender(<RuntimeHarness messages={sameLineageWithoutDuplicate} scopeKey="lineage-a" />)

    await waitFor(() =>
      expect(renderedMessages()).toEqual([
        { id: 'a', text: 'one' },
        {
          id: 'a:renderer-duplicate:2:renderer-duplicate:2',
          text: 'same-lineage legitimate id'
        }
      ])
    )

    const sameLineageRestored = [
      ...transcriptA.map(item => ({ ...item })),
      { ...sameLineageWithoutDuplicate[1] }
    ]

    rerender(<RuntimeHarness messages={sameLineageRestored} scopeKey="lineage-a" />)

    await waitFor(() =>
      expect(renderedMessages()).toEqual([
        { id: 'a', text: 'one' },
        { id: 'a:renderer-duplicate:2', text: 'two' },
        {
          id: 'a:renderer-duplicate:2:renderer-duplicate:2',
          text: 'same-lineage legitimate id'
        }
      ])
    )

    const transcriptB = [message('a:renderer-duplicate:2', 'user', 'unrelated lineage source id')]
    rerender(<RuntimeHarness messages={transcriptB} scopeKey="lineage-b" />)

    await waitFor(() =>
      expect(renderedMessages()).toEqual([
        { id: 'a:renderer-duplicate:2', text: 'unrelated lineage source id' }
      ])
    )
    expect(transcriptB[0]?.id).toBe('a:renderer-duplicate:2')
  })

  it('preserves duplicate-id messages with deterministic renderer-only ids across updates', () => {
    const malformed = [
      message('reused-id', 'user', 'first visible message'),
      message('assistant-id', 'assistant', 'reply between duplicates'),
      message('reused-id', 'user', 'second visible message'),
      message('reused-id:renderer-duplicate:2', 'assistant', 'valid suffix-shaped id')
    ]

    const { rerender } = render(<RuntimeHarness messages={malformed} />)
    const initial = renderedMessages()

    expect(initial.map(item => item.text)).toEqual([
      'first visible message',
      'reply between duplicates',
      'second visible message',
      'valid suffix-shaped id'
    ])
    expect(initial[0]?.id).toBe('reused-id')
    expect(initial[3]?.id).toBe('reused-id:renderer-duplicate:2')
    expect(new Set(initial.map(item => item.id)).size).toBe(initial.length)
    expect(malformed.map(item => item.id)).toEqual([
      'reused-id',
      'assistant-id',
      'reused-id',
      'reused-id:renderer-duplicate:2'
    ])

    rerender(<RuntimeHarness messages={malformed.map(item => ({ ...item }))} />)
    expect(renderedMessages()).toEqual(initial)

    rerender(
      <RuntimeHarness
        messages={[...malformed.map(item => ({ ...item })), message('streaming-id', 'assistant', 'streaming update')]}
      />
    )
    expect(renderedMessages().slice(0, initial.length)).toEqual(initial)
  })
})
