import { BrowserRouter as Router, Routes, Route } from "react-router-dom";
import CaseList from './ItemList';
import ItemDetails from './ItemDetails';
import './index.css';

export default function App() {
  return (
    <div className="font-inter bg-background text-foreground min-h-screen">  
      <Router>
        <Routes>
          <Route path="/" element={<CaseList />} />
          <Route path="/cases/:id" element={<ItemDetails />} />
        </Routes>
      </Router>
    </div>
  );
}