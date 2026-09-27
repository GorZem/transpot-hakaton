import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { MapPage } from './pages/MapPage'
import { SitePage } from './pages/SitePage'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<MapPage />} />
        <Route path="/sites/:id" element={<SitePage />} />
      </Routes>
    </BrowserRouter>
  )
}
