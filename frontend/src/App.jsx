import { useEffect, useRef, useState } from "react";
import {
  AppBar,
  Toolbar,
  Typography,
  Container,
  Grid,
  Card,
  CardContent,
  CardActions,
  Button,
  Chip,
  Stack,
} from "@mui/material";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import StopIcon from "@mui/icons-material/Stop";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import RefreshIcon from "@mui/icons-material/Refresh";
import { io } from "socket.io-client";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:5000";

function App() {
  const [apps, setApps] = useState([]);
  const [loading, setLoading] = useState(false);
  const socketRef = useRef(null);

  const updateFromServer = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/apps`);
      if (!res.ok) throw new Error("Failed to fetch status");
      const data = await res.json();
      setApps(data);
    } catch (err) {
      console.error(err);
    }
  };

  useEffect(() => {
    updateFromServer();
  }, []);

  useEffect(() => {
    const socket = io(API_BASE, { transports: ["websocket"] });
    socketRef.current = socket;

    socket.on("connect", () => {
      socket.emit("request_status");
    });

    socket.on("status_update", (payload) => {
      if (payload?.apps) {
        setApps(payload.apps);
      }
    });

    socket.on("disconnect", () => {
      console.warn("Socket disconnected");
    });

    return () => {
      socket.disconnect();
    };
  }, []);

  const performAction = async (appId, action) => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/${action}/${appId}`, {
        method: "POST",
      });
      if (!res.ok) {
        throw new Error(`${action} failed`);
      }
      const data = await res.json();
      if (data?.status) {
        setApps(data.status);
      }
    } catch (err) {
      console.error(err);
      updateFromServer();
    } finally {
      setLoading(false);
    }
  };

  const openApp = (url) => {
    window.open(url, "_blank", "noopener,noreferrer");
  };

  return (
    <>
      <AppBar position="static">
        <Toolbar>
          <Typography variant="h6" component="div">
            FlightDeck
          </Typography>
          <Button
            color="inherit"
            startIcon={<RefreshIcon />}
            onClick={updateFromServer}
            style={{ marginLeft: "auto" }}
            disabled={loading}
          >
            Refresh
          </Button>
        </Toolbar>
      </AppBar>

      <Container sx={{ marginTop: 4 }}>
        <Grid container spacing={3}>
          {apps.map((app) => (
            <Grid item xs={12} sm={6} md={4} key={app.id}>
              <Card>
                <CardContent>
                  <Stack
                    direction="row"
                    justifyContent="space-between"
                    alignItems="center"
                    spacing={1}
                  >
                    <Typography gutterBottom variant="h6" component="div">
                      {app.name}
                    </Typography>
                    <Chip
                      label={app.running ? "Running" : "Stopped"}
                      color={app.running ? "success" : "default"}
                      variant={app.running ? "filled" : "outlined"}
                      size="small"
                    />
                  </Stack>
                  <Typography variant="body2" color="text.secondary">
                    {app.url}
                  </Typography>
                  {app.pid && (
                    <Typography variant="caption" color="text.secondary">
                      PID: {app.pid}
                    </Typography>
                  )}
                </CardContent>
                <CardActions>
                  <Button
                    size="small"
                    startIcon={<PlayArrowIcon />}
                    disabled={loading || app.running}
                    onClick={() => performAction(app.id, "start")}
                  >
                    Start
                  </Button>
                  <Button
                    size="small"
                    color="error"
                    startIcon={<StopIcon />}
                    disabled={loading || !app.running}
                    onClick={() => performAction(app.id, "stop")}
                  >
                    Stop
                  </Button>
                  <Button
                    size="small"
                    startIcon={<OpenInNewIcon />}
                    onClick={() => openApp(app.url)}
                  >
                    Open
                  </Button>
                </CardActions>
              </Card>
            </Grid>
          ))}
        </Grid>
      </Container>
    </>
  );
}

export default App;
