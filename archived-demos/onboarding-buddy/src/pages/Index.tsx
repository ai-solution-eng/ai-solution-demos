
import { useEffect } from "react";
import { Navigate } from "react-router-dom";

const Index = () => {
  // Redirect to login page
  return <Navigate to="/login" />;
};

export default Index;
