import { useStore } from '@nanostores/react'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { getFleetStatus, getHermesConfigRecordForProfile, saveHermesConfigForProfile } from '@/hermes'
import { useI18n } from '@/i18n'
import { Activity, GitBranch, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { setFleetAutoComposerEnabled } from '@/store/session'
import type { FleetLaneEvaluation, FleetRoutePurpose, FleetStatusResponse, HermesConfigRecord } from '@/types/hermes'

import { Pill, SettingsContent, SettingsSection, ToggleRow } from './primitives'

const FLEET_SCHEMA_VERSION = 1

type LaneState = 'blocked' | 'fallback' | 'selectable'

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : String(error || '')
}

function isMissingFleetRoute(error: unknown): boolean {
  return /(?:^\s*|error:\s*)404\b/i.test(errorMessage(error))
}

function isSupportedPayload(payload: FleetStatusResponse): boolean {
  return (
    payload.schema_version === FLEET_SCHEMA_VERSION &&
    payload.command === 'doctor' &&
    Array.isArray(payload.evaluations)
  )
}

function laneState(evaluation: FleetLaneEvaluation): LaneState {
  if (evaluation.eligible) {
    return 'selectable'
  }

  if (evaluation.fallback_eligible) {
    return 'fallback'
  }

  return 'blocked'
}

function Detail({ label, mono = false, value }: { label: string; mono?: boolean; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">{label}</dt>
      <dd
        className={cn(
          'mt-1 min-w-0 break-words text-[length:var(--conversation-caption-font-size)] text-foreground',
          mono && 'font-mono'
        )}
      >
        {value}
      </dd>
    </div>
  )
}

function LaneRow({ evaluation, purpose }: { evaluation: FleetLaneEvaluation; purpose: FleetRoutePurpose }) {
  const { t } = useI18n()
  const copy = t.settings.providers.fleet
  const state = laneState(evaluation)
  const capacity = evaluation.capacity
  const laneName = copy.laneNames[evaluation.lane_id] ?? evaluation.lane_id
  const adapter = evaluation.adapter_kind === 'native_provider' ? copy.nativeProvider : copy.externalCli
  const purposeLabel = purpose === 'desktop_parent' ? copy.parent : copy.worker

  return (
    <article
      aria-label={`${laneName} · ${purposeLabel}`}
      className="border-t border-(--ui-stroke-tertiary) py-5 first:border-t-0 first:pt-1"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-[length:var(--conversation-text-font-size)] font-medium text-foreground">{laneName}</h3>
          <div className="mt-0.5 font-mono text-[0.68rem] text-(--ui-text-tertiary)">{evaluation.lane_id}</div>
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          <Pill tone={evaluation.enabled ? 'primary' : 'muted'}>
            {evaluation.enabled ? copy.enabled : copy.disabled}
          </Pill>
          <Pill>{purposeLabel}</Pill>
          <Pill tone={state === 'selectable' ? 'primary' : state === 'fallback' ? 'warn' : 'muted'}>
            {state === 'selectable' ? copy.selectable : state === 'fallback' ? copy.rotationFallback : copy.blocked}
          </Pill>
        </div>
      </div>

      {state === 'fallback' && (
        <p className="mt-3 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-yellow)">
          {copy.fallbackDetail}
        </p>
      )}

      <dl className="mt-4 grid gap-x-5 gap-y-3 sm:grid-cols-2 xl:grid-cols-4">
        <Detail label={copy.provider} mono value={evaluation.provider_id || copy.unavailable} />
        <Detail label={copy.model} mono value={evaluation.model_label || evaluation.model_id || copy.unavailable} />
        <Detail label={copy.effort} mono value={evaluation.effort || copy.unavailable} />
        <Detail label={copy.adapter} mono value={`${adapter} · ${evaluation.adapter_kind}`} />
        <Detail label={copy.remaining} value={capacity ? `${capacity.effective_remaining_pct}%` : copy.unavailable} />
        <Detail label={copy.freshness} mono value={capacity?.freshness ?? copy.unavailable} />
        <Detail label={copy.confidence} mono value={capacity?.confidence ?? copy.unavailable} />
        <Detail label={copy.capacitySource} mono value={capacity?.source_kind ?? copy.unavailable} />
      </dl>

      <div className="mt-4">
        <div className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
          {copy.reasons}
        </div>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {(evaluation.reasons.length > 0 ? evaluation.reasons : [copy.unknown]).map(reason => (
            <Pill key={reason}>
              <span className="font-mono">{reason}</span>
            </Pill>
          ))}
        </div>
      </div>

      <div className="mt-4 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height)">
        <div className="text-(--ui-text-tertiary)">{copy.qualification}</div>
        <p className="mt-1 whitespace-pre-wrap break-words text-(--ui-text-secondary)">
          {evaluation.qualification_detail || copy.noQualificationDetail}
        </p>
        {evaluation.qualification_evidence_id && (
          <p className="mt-1 break-all font-mono text-[0.68rem] text-(--ui-text-tertiary)">
            {copy.evidence}: {evaluation.qualification_evidence_id}
          </p>
        )}
      </div>
    </article>
  )
}

function Failure({
  description,
  onRetry,
  refreshing,
  title
}: {
  description: string
  onRetry: () => void
  refreshing: boolean
  title: string
}) {
  const { t } = useI18n()
  const copy = t.settings.providers.fleet

  return (
    <SettingsContent>
      <ErrorState className="min-h-72 place-content-center py-10" description={description} title={title}>
        <div className="flex justify-center">
          <Button disabled={refreshing} onClick={onRetry} size="sm" type="button" variant="secondary">
            <RefreshCw className={cn(refreshing && 'animate-spin')} />
            {refreshing ? copy.refreshing : copy.tryAgain}
          </Button>
        </div>
      </ErrorState>
    </SettingsContent>
  )
}

export function FleetRouterSettings() {
  const { t } = useI18n()
  const copy = t.settings.providers.fleet
  const activeProfile = normalizeProfileKey(useStore($activeGatewayProfile))
  const [savingAuto, setSavingAuto] = useState(false)

  const query = useQuery({
    queryFn: () => getFleetStatus(activeProfile),
    queryKey: ['fleet-status', activeProfile],
    refetchOnMount: 'always',
    retry: false,
    staleTime: 0
  })

  if (query.isLoading) {
    return (
      <SettingsContent>
        <div className="grid min-h-72 place-content-center justify-items-center gap-3 py-10 text-center">
          <Loader label={copy.checking} type="lemniscate-bloom" />
          <p className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
            {copy.checking}
          </p>
        </div>
      </SettingsContent>
    )
  }

  const unsupported = query.error
    ? isMissingFleetRoute(query.error)
    : Boolean(query.data && !isSupportedPayload(query.data))

  if (unsupported) {
    return (
      <Failure
        description={copy.unsupportedDescription}
        onRetry={() => void query.refetch()}
        refreshing={query.isFetching}
        title={copy.unsupportedTitle}
      />
    )
  }

  if (query.error || !query.data) {
    return (
      <Failure
        description={copy.loadFailedDescription(errorMessage(query.error))}
        onRetry={() => void query.refetch()}
        refreshing={query.isFetching}
        title={copy.loadFailedTitle}
      />
    )
  }

  const payload = query.data
  const workerStatus = payload.purposes?.task_worker
  const parentStatus = payload.purposes?.desktop_parent
  const workerEvaluations = workerStatus?.evaluations ?? payload.evaluations
  const parentEvaluations = parentStatus?.evaluations ?? []
  const parentEnabled = parentStatus?.enabled ?? false
  const fallbackCount = payload.evaluations.filter(item => item.fallback_eligible).length

  const setParentAuto = async (enabled: boolean) => {
    const profile = activeProfile
    setSavingAuto(true)

    try {
      const current = await getHermesConfigRecordForProfile(profile)

      const currentFleet =
        current.fleet && typeof current.fleet === 'object' ? (current.fleet as Record<string, unknown>) : {}

      const next: HermesConfigRecord = {
        ...current,
        fleet: {
          ...currentFleet,
          ...(enabled ? { enabled: true } : {}),
          parent_desktop_enabled: enabled
        }
      }

      await saveHermesConfigForProfile(next, profile)

      if (normalizeProfileKey($activeGatewayProfile.get()) === profile) {
        setFleetAutoComposerEnabled(enabled)
      }

      await query.refetch()
    } catch (error) {
      notifyError(error, copy.parentAutoFailed)
    } finally {
      setSavingAuto(false)
    }
  }

  const summary = !payload.enabled
    ? copy.fleetDisabledDetail
    : payload.ok
      ? copy.healthyDetail
      : fallbackCount > 0
        ? copy.fallbackSummary
        : copy.attentionDetail

  return (
    <SettingsContent>
      <SettingsSection
        aside={
          <Button
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
            size="sm"
            type="button"
            variant="secondary"
          >
            <RefreshCw className={cn(query.isFetching && 'animate-spin')} />
            {query.isFetching ? copy.refreshing : copy.refresh}
          </Button>
        }
        icon={GitBranch}
        title={copy.title}
      >
        <p className="max-w-3xl text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
          {copy.intro}
        </p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          <Pill tone={payload.enabled ? 'primary' : 'muted'}>
            {payload.enabled ? copy.fleetEnabled : copy.fleetDisabled}
          </Pill>
          <Pill tone={payload.ok ? 'primary' : 'warn'}>{payload.ok ? copy.healthy : copy.attention}</Pill>
          <Pill>
            <span className="font-mono">{payload.reason}</span>
          </Pill>
        </div>
        <p className="mt-3 max-w-3xl text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-secondary)">
          {summary}
        </p>
        <div className="mt-4 border-t border-(--ui-stroke-tertiary)">
          <ToggleRow
            checked={parentEnabled}
            description={copy.parentAutoDescription}
            disabled={savingAuto}
            label={copy.parentAuto}
            onChange={enabled => void setParentAuto(enabled)}
          />
        </div>
      </SettingsSection>

      {parentEvaluations.length > 0 && (
        <SettingsSection icon={Activity} meta={String(parentEvaluations.length)} title={copy.parentLanesTitle}>
          <div>
            {parentEvaluations.map(evaluation => (
              <LaneRow evaluation={evaluation} key={`parent:${evaluation.lane_id}`} purpose="desktop_parent" />
            ))}
          </div>
        </SettingsSection>
      )}

      <SettingsSection icon={Activity} meta={String(workerEvaluations.length)} title={copy.workerLanesTitle}>
        <div>
          {workerEvaluations.map(evaluation => (
            <LaneRow evaluation={evaluation} key={`worker:${evaluation.lane_id}`} purpose="task_worker" />
          ))}
        </div>
      </SettingsSection>
    </SettingsContent>
  )
}
