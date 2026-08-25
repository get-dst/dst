import { createContext, useContext, type ReactNode } from 'react'

interface TabsContextValue {
  active: string
  setActive: (value: string) => void
}

const TabsContext = createContext<TabsContextValue | null>(null)

function useTabsContext() {
  const ctx = useContext(TabsContext)
  if (!ctx) throw new Error('Tabs sub-components must be used inside <Tabs>')
  return ctx
}

interface TabsProps {
  value: string
  onValueChange: (value: string) => void
  children: ReactNode
  className?: string
}

export function Tabs({ value, onValueChange, children, className = '' }: TabsProps) {
  return (
    <TabsContext.Provider value={{ active: value, setActive: onValueChange }}>
      <div className={className}>{children}</div>
    </TabsContext.Provider>
  )
}

interface TabListProps {
  children: ReactNode
  className?: string
}

export function TabList({ children, className = '' }: TabListProps) {
  return (
    <div
      role="tablist"
      className={[
        'flex items-center gap-0 border-b border-border',
        className,
      ].join(' ')}
    >
      {children}
    </div>
  )
}

interface TabTriggerProps {
  value: string
  children: ReactNode
  className?: string
}

export function TabTrigger({ value, children, className = '' }: TabTriggerProps) {
  const { active, setActive } = useTabsContext()
  const isActive = active === value

  return (
    <button
      role="tab"
      aria-selected={isActive}
      type="button"
      onClick={() => setActive(value)}
      className={[
        'relative px-4 py-2.5 text-[13px] font-medium transition-colors cursor-pointer',
        'outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset',
        isActive
          ? 'text-text'
          : 'text-muted hover:text-text hover:bg-surface-2 rounded-t-md',
        className,
      ].join(' ')}
      style={{ transitionDuration: 'var(--duration-fast)' }}
    >
      {children}
      {/* Active underline */}
      {isActive && (
        <span
          className="absolute bottom-0 left-0 right-0 h-[2px] bg-accent rounded-t-full"
          aria-hidden="true"
        />
      )}
    </button>
  )
}

interface TabPanelProps {
  value: string
  children: ReactNode
  className?: string
}

export function TabPanel({ value, children, className = '' }: TabPanelProps) {
  const { active } = useTabsContext()
  if (active !== value) return null
  return (
    <div role="tabpanel" className={className}>
      {children}
    </div>
  )
}
