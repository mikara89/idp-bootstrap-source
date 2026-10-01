const http = require('node:http');
const port = Number(process.env.PORT || 3000);
const healthPath = process.env.HEALTH_PATH || '/healthz';
const readinessPath = process.env.READINESS_PATH || '/readyz';
const metricsPath = process.env.METRICS_PATH || '/metrics';
const metrics = '# HELP app_info Reference service information\n# TYPE app_info gauge\napp_info{service="${{ values.name }}"} 1\n';
const server = http.createServer((request, response) => {
  const bodies = {'/': '${{ values.name }} is running\n', [healthPath]: 'ok\n', [readinessPath]: 'ready\n', [metricsPath]: metrics};
  if (!(request.url in bodies)) { response.writeHead(404); return response.end('not found\n'); }
  response.writeHead(200, {'content-type': request.url === '/metrics' ? 'text/plain; version=0.0.4' : 'text/plain'});
  response.end(bodies[request.url]);
});
if (require.main === module) server.listen(port, '0.0.0.0', () => console.log('${{ values.name }} listening on ' + port));
module.exports = server;
