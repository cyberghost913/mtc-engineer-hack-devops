require 'net/http'
require 'json'

# Do not use HTTP_PROXY for the loopback-only monitor endpoint.
http = Net::HTTP.new('127.0.0.1', 24220, nil)
http.open_timeout = 2
http.read_timeout = 2
response = http.get('/api/plugins.json')
abort 'monitor_agent is not healthy' unless response.is_a?(Net::HTTPSuccess)
ids = JSON.parse(response.body).fetch('plugins').map { |plugin| plugin['plugin_id'] }
abort 'collector plugins are missing' unless %w[nginx_tail nginx_archive].all? { |id| ids.include?(id) }
