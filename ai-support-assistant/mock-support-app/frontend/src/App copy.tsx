import { useState } from 'react'
import reactLogo from './assets/react.svg'
import viteLogo from '/vite.svg'
import './App.css'

export default function App() {
  return (
    <div className="min-h-screen bg-green-50 text-gray-800 p-6">
      <h1 className="text-3xl font-bold mb-4">Tailwind 4 + Vite + React</h1>
      <div className="p-4 bg-white rounded shadow">
        Tailwind CSS is working 🎉
      </div>
      <div>
        <table class="border">
          <tr class="border">
            <th class="bg-[#dbeafe] font-bold border">This should have a blue background.</th>
            <th class="bg-blue-100 font-bold border">This should have a blue background.</th>
          </tr>
        </table>
        <button class="bg-sky-500/100">Click Me</button>
        <div className="font-inter text-gray-900">
          This text uses the Inter font.
        </div>        
      </div>
    </div>
  );
}
