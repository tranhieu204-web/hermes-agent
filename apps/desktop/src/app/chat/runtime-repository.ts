import { ExportedMessageRepository, type ThreadMessage } from '@assistant-ui/react'
import { useMemo, useRef } from 'react'

import type { ChatMessage } from '@/lib/chat-messages'
import { coalesceToolOnlyAssistants, createToolMergeCache, toRuntimeMessage } from '@/lib/chat-runtime'

/**
 * ChatMessage[] -> assistant-ui message repository, with a WeakMap identity
 * cache so unchanged messages convert once (and a tool-merge cache that folds
 * tool-only assistant turns into their neighbour). Shared by the main chat's
 * runtime boundary and session tiles — one transcript pipeline, N surfaces.
 */
export function useRuntimeMessageRepository(
  messages: ChatMessage[],
  repositoryScopeKey: string
): ExportedMessageRepository {
  const cacheRef = useRef(new WeakMap<ChatMessage, ThreadMessage>())
  const toolMergeCacheRef = useRef(createToolMergeCache())

  // Source-id occurrence slots survive clone-based rerenders. Once a slot has a
  // renderer id, it keeps it so a later source id cannot steal that identity.
  // Reservations belong to one durable transcript lineage, not to this hook's
  // lifetime: ChatRuntimeBoundary survives navigation between sessions.
  const repositoryIdStateRef = useRef({
    scopeKey: repositoryScopeKey,
    idsBySource: new Map<string, string[]>()
  })

  if (repositoryIdStateRef.current.scopeKey !== repositoryScopeKey) {
    repositoryIdStateRef.current = {
      scopeKey: repositoryScopeKey,
      idsBySource: new Map<string, string[]>()
    }
  }

  const repositoryIdsBySource = repositoryIdStateRef.current.idsBySource

  return useMemo(() => {
    const items: { message: ThreadMessage; parentId: string | null }[] = []
    const branchParentByGroup = new Map<string, string | null>()
    const mergedMessages = coalesceToolOnlyAssistants(messages, toolMergeCacheRef.current)
    const sourceIds = new Set(mergedMessages.map(message => message.id))
    const occurrenceBySource = new Map<string, number>()

    const slots = mergedMessages.map(message => {
      const occurrence = occurrenceBySource.get(message.id) ?? 0
      occurrenceBySource.set(message.id, occurrence + 1)

      return {
        occurrence,
        previousRepositoryId: repositoryIdsBySource.get(message.id)?.[occurrence]
      }
    })

    // Historical projected identities stay reserved even while their source slot
    // is absent, so a new source id cannot steal an identity that may return.
    const repositoryIds = new Set([...repositoryIdsBySource.values()].flat())

    let visibleParentId: string | null = null
    let headId: string | null = null

    for (const [index, message] of mergedMessages.entries()) {
      const slot = slots[index]
      let repositoryId = slot?.previousRepositoryId ?? message.id

      if (!slot?.previousRepositoryId && repositoryIds.has(repositoryId)) {
        let collision = 2

        do {
          repositoryId = `${message.id}:renderer-duplicate:${collision}`
          collision += 1
        } while (sourceIds.has(repositoryId) || repositoryIds.has(repositoryId))
      }

      repositoryIds.add(repositoryId)

      if (!slot?.previousRepositoryId) {
        const assignments = repositoryIdsBySource.get(message.id) ?? []
        assignments[slot?.occurrence ?? 0] = repositoryId
        repositoryIdsBySource.set(message.id, assignments)
      }

      let parentId = visibleParentId

      if (message.role === 'assistant' && message.branchGroupId) {
        if (!branchParentByGroup.has(message.branchGroupId)) {
          branchParentByGroup.set(message.branchGroupId, visibleParentId)
        }

        parentId = branchParentByGroup.get(message.branchGroupId) ?? null
      }

      const cachedMessage = cacheRef.current.get(message)
      const convertedMessage = cachedMessage ?? toRuntimeMessage(message)

      const runtimeMessage =
        repositoryId === convertedMessage.id ? convertedMessage : { ...convertedMessage, id: repositoryId }

      if (!cachedMessage) {
        cacheRef.current.set(message, convertedMessage)
      }

      items.push({ message: runtimeMessage, parentId })

      if (!message.hidden) {
        visibleParentId = repositoryId
        headId = repositoryId
      }
    }

    return ExportedMessageRepository.fromBranchableArray(items, { headId })
  }, [messages, repositoryIdsBySource])
}
