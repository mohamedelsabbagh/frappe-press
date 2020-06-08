import call from '../controllers/call';

export default class ResourceManager {
	constructor(vm, resourceDefs) {
		this._vm = vm;
		this._watchers = [];
		let resources = {};
		for (let key in resourceDefs) {
			let resourceDef = resourceDefs[key];
			if (typeof resourceDef === 'function') {
				this._watchers.push([
					() => resourceDef.call(vm),
					(n, o) => this.updateResource(key, n, o),
					{
						immediate: true,
						deep: true,
						sync: true
					}
				]);
			} else {
				let resource = new Resource(vm, resourceDef);
				resources[key] = resource;

				if (resource.auto) {
					resource.reload();
				}
			}
		}
		this.resources = resources;
	}

	init() {
		this._watchers = this._watchers.map(w => this._vm.$watch(...w));
	}

	destroy() {
		const vm = this._vm;

		// this.cancelAll();
		// Object.values(this.resources).forEach(r => {
		// 	r.stopInterval();
		// });
		delete vm._rm;
	}

	updateResource(key, newValue, oldValue) {
		let resource;
		if (key in this.resources) {
			resource = this.resources[key];
		} else {
			resource = new Resource(this._vm, newValue);
			this._vm.$set(this.resources, key, resource);
		}

		let oldData = resource.data;

		// cancel existing fetches
		if (oldValue && resource) {
			resource.cancel();
		}

		resource.update(newValue);
		// keep data if it is needed between refreshes
		if (resource.keepData) {
			resource.data = oldData;
		}

		if (resource.auto) {
			resource.reload();
		}
	}
}

class Resource {
	constructor(vm, options = {}, initialValue) {
		if (typeof options == 'string') {
			options = { method: options, auto: true };
		}
		if (!options.method) {
			throw new Error(
				'[Resource Manager]: method is required to define a resource'
			);
		}
		this._vm = vm;
		this.method = options.method;
		this.update(options, initialValue);
	}

	update(options, initialValue) {
		if (typeof options == 'string') {
			options = { method: options, auto: true };
		}
		if (this.method && options.method && options.method !== this.method) {
			throw new Error(
				'[Resource Manager]: method cannot change for the same resource'
			);
		}
		// params
		this.params = options.params || null;
		this.auto = options.auto || false;
		this.keepData = options.keepData || false;
		this.condition = options.condition || (() => true);
		this.paged = options.paged || false;

		// events
		this.listeners = Object.create(null);
		this.onceListeners = Object.create(null);
		let listenerKeys = Object.keys(options).filter(key => key.startsWith('on'));
		if (listenerKeys.length > 0) {
			for (const key of listenerKeys) {
				this.on(key, options[key]);
			}
		}

		// response
		this.data = initialValue || options.default || null;
		this.error = null;
		this.loading = false;
		this.lastLoaded = null;
	}

	async fetch() {
		if (!this.condition()) return;

		this.loading = true;
		try {
			let data = await call(this.method, this.params);
			if (Array.isArray(data) && this.paged) {
				this.data = [].concat(this.data || [], data);
			} else {
				this.data = data;
			}
			this.emit('Success', this.data);
		} catch (error) {
			this.error = error.messages.join('\n');
			this.emit('Error', this.error);
		}
		this.lastLoaded = new Date();
		this.loading = false;
	}

	reload() {
		return this.fetch();
	}

	submit() {
		return this.fetch();
	}

	cancel() {}

	on(event, handler) {
		this.listeners[event] = (this.listeners[event] || []).concat(handler);
		return this;
	}

	once(event, handler) {
		this.onceListeners[event] = (this.onceListeners[event] || []).concat(
			handler
		);
		return this;
	}

	emit(event, ...args) {
		let key = 'on' + event;
		let vm = this._vm;

		(this.listeners[key] || []).forEach(handler => {
			runHandler(handler);
		});
		(this.onceListeners[key] || []).forEach(handler => {
			runHandler(handler);
			// remove listener after calling handler
			this.onceListeners[key].splice(
				this.onceListeners[key].indexOf(handler),
				1
			);
		});

		function runHandler(handler) {
			try {
				handler.call(vm, ...args);
			} catch (error) {
				console.error(error);
			}
		}
	}
}
