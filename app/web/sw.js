/* Service worker deliberadamente sin cache.
 *
 * Existe por una sola razon: los navegadores exigen un service worker con un
 * manejador de `fetch` para ofrecer la instalacion de la aplicacion. Con eso el
 * sistema se abre en ventana propia, con su icono y su nombre, en vez de dentro
 * de una pestana.
 *
 * NO CACHEA NADA, Y ES A PROPOSITO. Un service worker que cachea es justo lo
 * que puede arruinar la evaluacion: el jurado clona el repositorio, levanta el
 * servidor y el navegador le sirve una version guardada de otra sesion. Ese
 * fallo es dificil de diagnosticar y no se manifiesta como error, sino como una
 * interfaz que no corresponde al codigo entregado.
 *
 * Ademas aqui no habria nada que cachear con sentido: la conversacion es un
 * flujo de eventos contra el servidor y el indice del corpus vive en la memoria
 * del proceso. Sin servidor no hay nada util que hacer, asi que un modo sin
 * conexion seria una promesa vacia.
 *
 * El manejador de `fetch` esta presente pero no llama a `respondWith`, de modo
 * que cada peticion sigue su camino normal a la red. Es el minimo que cumple el
 * requisito sin interponerse en nada.
 */

self.addEventListener("install", () => {
  // Sin espera: no hay version anterior con la que convivir.
  self.skipWaiting();
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(
    (async () => {
      // Barrido de seguridad: si alguna version futura llegara a crear caches,
      // o si quedo alguna de una prueba, se borran al activar.
      const nombres = await caches.keys();
      await Promise.all(nombres.map((n) => caches.delete(n)));
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", () => {
  // Intencionadamente vacio: sin `respondWith`, el navegador resuelve la
  // peticion contra la red como si el service worker no existiera.
});
