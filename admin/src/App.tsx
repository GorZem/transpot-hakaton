import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { MapPage } from './pages/MapPage'
import { MonitoringPage } from './pages/MonitoringPage'
import { SettingsPage } from './pages/SettingsPage'
import { StatsPage } from './pages/StatsPage'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/monitoring" replace />} />
        <Route path="/monitoring/:id?" element={<MonitoringPage />} />
        <Route path="/map" element={<MapPage />} />
        <Route path="/stats/:id?" element={<StatsPage />} />
        <Route path="/settings/:id?" element={<SettingsPage />} />
        <Route path="/sites/:id" element={<Navigate to="/monitoring" replace />} />
        <Route path="*" element={<Navigate to="/monitoring" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
