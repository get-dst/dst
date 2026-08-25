import { Routes, Route } from 'react-router-dom'
import { Header } from './components/Header'
import { Lenses } from './pages/Lenses'
import { LensDetail } from './pages/LensDetail'
import { DataSources } from './pages/DataSources'
import { Observe } from './pages/Observe'
import { Certify } from './pages/Certify'
import { Router as RouterPage } from './pages/Router'
import { Settings } from './pages/Settings'

export default function App({ clerkEnabled = false }: { clerkEnabled?: boolean }) {
  return (
    <div className="flex h-full flex-col">
      <Header clerkEnabled={clerkEnabled} />
      <main className="flex-1 overflow-auto">
        <Routes>
          <Route path="/" element={<Lenses />} />
          <Route path="/lenses/:name" element={<LensDetail />} />
          <Route path="/data-sources" element={<DataSources />} />
          {/* Alias: /integrations resolves to Data sources. */}
          <Route path="/integrations" element={<DataSources />} />
          <Route path="/observe" element={<Observe />} />
          <Route path="/certify" element={<Certify />} />
          {/* The drift audit is a Certify tab; /audit deep-links to it. */}
          <Route path="/audit" element={<Certify initialTab="drift" />} />
          <Route path="/router" element={<RouterPage />} />
          {/* Reviews live under Observe; keep the routes so review tracking URLs resolve. */}
          <Route path="/reviews" element={<Observe initialTab="reviews" />} />
          <Route path="/reviews/:id" element={<Observe initialTab="reviews" />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  )
}
