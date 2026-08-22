import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import axios from 'axios'
import { API_BASE } from './config'
import './index.css'
import App from './App.jsx'

if (API_BASE) {
  axios.defaults.baseURL = API_BASE
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
