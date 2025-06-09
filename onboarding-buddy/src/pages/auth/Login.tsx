
import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";

const Login: React.FC = () => {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const { login } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);

    try {
      await login(email, password);
      navigate("/dashboard");
    } catch (error) {
      // Error is already displayed by the AuthContext
    } finally {
      setIsLoading(false);
    }
  };

  const handleDemoLogin = async (role: string) => {
    let demoEmail = "";
    
    switch (role) {
      case "admin":
        demoEmail = "admin@example.com";
        break;
      case "manager":
        demoEmail = "manager@example.com";
        break;
      case "mentor":
        demoEmail = "mentor@example.com";
        break;
      case "new-hire":
        demoEmail = "newhire@example.com";
        break;
      case "jan":
        demoEmail = "jan@example.com";
        break;
      case "claudio":
        demoEmail = "claudio@example.com";
        break;
      case "janna":
        demoEmail = "janna@example.com";
        break;
      default:
        break;
    }
    
    setIsLoading(true);
    
    try {
      await login(demoEmail, "password");
      navigate("/dashboard");
    } catch (error) {
      // Error is already displayed by the AuthContext
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="flex items-center justify-center min-h-screen bg-background p-4">
      <div className="w-full max-w-md space-y-8">
        <div className="text-center">
          <h1 className="text-3xl font-bold">Onboarding Buddy</h1>
          <p className="text-muted-foreground">Employee Onboarding Platform</p>
        </div>
        
        <Card>
          <CardHeader>
            <CardTitle>Log in</CardTitle>
            <CardDescription>
              Enter your email to sign in to your account
            </CardDescription>
          </CardHeader>
          <form onSubmit={handleSubmit}>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="email">Email</Label>
                <Input
                  id="email"
                  type="email" 
                  placeholder="name@example.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label htmlFor="password">Password</Label>
                </div>
                <Input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </div>
            </CardContent>
            <CardFooter className="flex flex-col">
              <Button
                type="submit"
                className="w-full"
                disabled={isLoading}
              >
                {isLoading ? "Signing in..." : "Sign in"}
              </Button>
            </CardFooter>
          </form>
        </Card>
        
        <div className="mt-4">
          <p className="text-center text-sm text-muted-foreground mb-4">
            Demo accounts (click to login):
          </p>
          <div className="grid grid-cols-2 gap-2">
            <Button 
              variant="outline" 
              onClick={() => handleDemoLogin("admin")}
              className="text-xs"
            >
              Admin User
            </Button>
            <Button 
              variant="outline" 
              onClick={() => handleDemoLogin("manager")}
              className="text-xs"
            >
              Manager User
            </Button>
            <Button 
              variant="outline" 
              onClick={() => handleDemoLogin("mentor")}
              className="text-xs"
            >
              Mentor User
            </Button>
            <Button 
              variant="outline" 
              onClick={() => handleDemoLogin("new-hire")}
              className="text-xs"
            >
              New Hire
            </Button>
            <Button 
              variant="outline" 
              onClick={() => handleDemoLogin("jan")}
              className="text-xs"
            >
              Jan
            </Button>
            <Button 
              variant="outline" 
              onClick={() => handleDemoLogin("claudio")}
              className="text-xs"
            >
              Claudio
            </Button>
            <Button 
              variant="outline" 
              onClick={() => handleDemoLogin("janna")}
              className="text-xs"
            >
              Janna
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default Login;
