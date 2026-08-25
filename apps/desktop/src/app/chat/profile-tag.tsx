import { useStore } from '@nanostores/react'

import { ProfileGlyph } from '@/components/ui/profile-glyph'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { resolveProfileColor } from '@/lib/profile-color'
import { $profileColors, $profiles, normalizeProfileKey, profileLabel } from '@/store/profile'

/** Owning-profile chip: the shared {@link ProfileGlyph}, resolved against the
 *  live profile colors and labelled with the full name. Identity, not status —
 *  session state dots keep their own semantics (#66003). */
export function ProfileTag({
  className,
  expanded = false,
  profile
}: {
  className?: string
  expanded?: boolean
  profile: null | string | undefined
}) {
  const { t } = useI18n()
  const colors = useStore($profileColors)
  const profiles = useStore($profiles)
  const key = normalizeProfileKey(profile)
  const info = profiles.find(candidate => normalizeProfileKey(candidate.name) === key)
  const displayName = info ? profileLabel(info) : key
  const ownerName = `${displayName} · ${key}`
  const label = t.sidebar.row.ownedByProfile(key)
  const expandedLabel = t.sidebar.row.ownedByProfile(ownerName)

  const glyph = (
    <ProfileGlyph
      aria-label={label}
      className={className}
      color={resolveProfileColor(key, colors)}
      isDefault={key === 'default'}
      name={key}
      role="img"
    />
  )

  if (expanded) {
    return (
      <span
        aria-label={expandedLabel}
        className="inline-flex min-w-0 items-center gap-1 text-[0.6875rem] text-(--ui-text-tertiary)"
      >
        {glyph}
        <span className="min-w-0 max-w-20 truncate">{displayName}</span>
        <span className="shrink-0">· {key}</span>
      </span>
    )
  }

  return <Tip label={label}>{glyph}</Tip>
}
