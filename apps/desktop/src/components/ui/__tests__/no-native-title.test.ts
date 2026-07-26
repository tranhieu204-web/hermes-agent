import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import { Tip } from '../tooltip'

afterEach(cleanup)

describe('themed button tips', () => {
  it('renders an instant themed tip without exposing a native title attribute', async () => {
    render(
      createElement(Tip, {
        label: 'Open settings',
        children: createElement('button', { 'aria-label': 'Open settings', type: 'button' }, 'Settings')
      })
    )

    const button = screen.getByRole('button', { name: 'Open settings' })
    expect(button.getAttribute('title')).toBeNull()

    fireEvent.pointerMove(button)

    const tip = await screen.findByRole('tooltip')
    expect(tip.textContent).toContain('Open settings')
    expect(tip.getAttribute('data-slot')).toBe('tooltip-content')
    expect(button.getAttribute('title')).toBeNull()
  })
})
