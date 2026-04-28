import { BrowserRouter as Router, Routes, Route } from "react-router-dom";
import Layout from "@/components/Layout";
import CaseList from './ItemList';
import ItemDetails from './ItemDetails';
import './index.css';

export default function App() {
  return (
    <div className="font-inter bg-background text-foreground min-h-screen">  
      <Router>
        <Routes>
          <Route path="/" element={<Layout />}>
            <Route path="/" element={<CaseList />} />
            <Route path="/cases/:id" element={<ItemDetails />} />
          </Route>
        </Routes>
      </Router>
    </div>
  );
}