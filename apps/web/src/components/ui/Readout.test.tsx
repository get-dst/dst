// The readout grammar: one component renders every label·value machine-fact
// line. These pin it so the fascia can't silently lose its instruments in a
// refactor.
import { expect, test } from 'vitest'
import { render } from '@testing-library/react'
import { Readout } from './Readout'

test('renders label value pairs with dot separators in mono', () => {
  const { container } = render(
    <Readout items={[{ value: '12 lenses' }, { label: 'queries', value: '4,210' }]} />,
  )
  expect(container.textContent).toBe('12 lenses · queries 4,210')
  expect(container.querySelector('.font-mono')).not.toBeNull()
})

test('empty values are dropped and an all-empty readout renders nothing', () => {
  const { container } = render(
    <Readout items={[{ label: 'a', value: '' }, { label: 'b', value: 'x' }]} />,
  )
  expect(container.textContent).toBe('b x')
  const empty = render(<Readout items={[{ label: 'a', value: '' }]} />)
  expect(empty.container.textContent).toBe('')
})
