import React from 'react'
import ReactDOM from 'react-dom/client'
import { SimpleApp } from './simple/SimpleApp'
import './simple/simple.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <SimpleApp />
  </React.StrictMode>,
)
