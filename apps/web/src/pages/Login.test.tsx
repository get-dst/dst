import { test, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Login } from './Login'

test('renders email+password sign-in with the token escape hatch', () => {
  render(<Login />)
  expect(screen.getByText('dst')).toBeInTheDocument()
  expect(screen.getByLabelText('Email')).toBeInTheDocument()
  expect(screen.getByLabelText('Password')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Use an admin token instead' }))
  expect(screen.getByLabelText('Admin token')).toBeInTheDocument()
  expect(screen.getByPlaceholderText('dstadm_…')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Sign in with email instead' }))
  expect(screen.getByLabelText('Email')).toBeInTheDocument()
})
