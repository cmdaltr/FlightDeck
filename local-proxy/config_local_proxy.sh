cp FlightDeck/local-proxy/com.local.caddy.* ~/Library/LaunchAgents
ben@MacBook GitHub % launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.caddy.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.caddy.config.plist

# FlightDeck auto-start on login
cp FlightDeck/local-proxy/com.local.flightdeck.plist ~/Library/LaunchAgents
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.flightdeck.plist