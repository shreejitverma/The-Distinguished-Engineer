#include <atomic>
#include <chrono>
#include <iostream>
#include <cstddef>
#include <cstdint>
#include <thread>
#include <typeinfo>
#include <vector>
#include <cxxabi.h>
#include <dlfcn.h>

// Itanium C++ ABI specification structures (Section 2.9.4)
namespace itanium_abi {
    struct __class_type_info : public std::type_info {};

    struct __base_class_type_info {
        const __class_type_info* __base_type;
        long __offset_flags;

        enum __offset_flags_masks {
            __virtual_mask = 0x1,
            __public_mask = 0x2,
            __offset_shift = 8
        };

        ptrdiff_t offset() const {
            return __offset_flags >> __offset_shift;
        }
        bool is_public() const {
            return (__offset_flags & __public_mask) != 0;
        }
        bool is_virtual() const {
            return (__offset_flags & __virtual_mask) != 0;
        }
    };

    struct __vmi_class_type_info : public __class_type_info {
        unsigned int __flags;
        unsigned int __base_count;
        __base_class_type_info __base_info[1];
    };
}

// 1. Classes demonstrating Multiple Inheritance, EBO, and Tail Padding
struct EmptyTag {};

// Probe for the empty base optimization: with EBO the empty base adds no bytes.
struct EboProbe : EmptyTag {
    int x;
};

struct NonPodBase {
    void* ptr;
    int a;
    NonPodBase() : ptr(nullptr), a(1) {}
    ~NonPodBase() {}
};

struct PodBase {
    void* ptr;
    int a;
};

struct DerivedTailReuse : NonPodBase {
    int b;
};

struct DerivedPodNoReuse : PodBase {
    int b;
};

struct Base1 {
    virtual ~Base1() = default;
    virtual uintptr_t f1() { return reinterpret_cast<uintptr_t>(this); }
    int b1 = 10;
};

struct Base2 {
    virtual ~Base2() = default;
    virtual uintptr_t f2() { return reinterpret_cast<uintptr_t>(this); }
    int b2 = 20;
};

struct MultiDerived : EmptyTag, Base1, Base2 {
    uintptr_t f1() override { return reinterpret_cast<uintptr_t>(this); }
    uintptr_t f2() override { return reinterpret_cast<uintptr_t>(this); }
    virtual void f3() {}
    int md = 30;
};

// 2. Diamond Virtual Inheritance
struct VBase {
    virtual ~VBase() = default;
    virtual int vval() { return 100; }
    virtual uintptr_t vthis() { return reinterpret_cast<uintptr_t>(this); }
    int vdata = 999;
};

// Records the vptr that DiamondB1's own constructor sees while it runs as a base subobject.
// That vptr points into a construction vtable (_ZTC...), not into the final DiamondDerived vtable.
void** g_b1_ctor_vptr = nullptr;

struct DiamondB1 : virtual VBase {
    int b1val = 11;
    DiamondB1() { g_b1_ctor_vptr = *reinterpret_cast<void***>(this); }
};

struct DiamondB2 : virtual VBase {
    int b2val = 22;
};

struct DiamondDerived : DiamondB1, DiamondB2 {
    int dval = 33;
    // VBase lives at a virtual offset, so VBase's vtable slot for this override is a virtual thunk (_ZTv...).
    uintptr_t vthis() override { return reinterpret_cast<uintptr_t>(this); }
};

// 3. Covariant return with a non-zero return adjustment
struct RetA {
    virtual ~RetA() = default;
    int ra = 1;
};
struct RetB {
    virtual ~RetB() = default;
    int rb = 2;
};
struct RetD : RetA, RetB {};

struct CovBase {
    virtual ~CovBase() = default;
    virtual RetB* get() { return nullptr; }
};
// RetD* converts to RetB* by adding the offset of the RetB subobject, so CovBase::get's slot
// holds a covariant thunk (_ZTch...) that adjusts the returned pointer.
struct CovDerived : CovBase {
    RetD rd;
    RetD* get() override { return &rd; }
};

// 4. Thread-safe static local with a dynamic initializer (needs a guard variable and
// __cxa_guard_acquire / __cxa_guard_release). The slow initializer widens the race window.
std::atomic<int> g_init_runs{0};
int slow_init() {
    g_init_runs.fetch_add(1);
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    return 42;
}
int get_guarded_static() {
    static int x = slow_init();
    return x;
}

int main() {
    std::cout << "=== Itanium C++ ABI Object Model and Vtable Inspection ===\n";

    // Section 1: Object Layout and Empty Base Optimization
    std::cout << "[Layout] MultiDerived sizeof: " << sizeof(MultiDerived) << "\n";
    std::cout << "[Layout] Empty base optimization active (0 extra bytes): "
              << (sizeof(EmptyTag) == 1 && sizeof(EboProbe) == sizeof(int) ? "PASS" : "FAIL") << "\n";
    std::cout << "[Layout] Non-POD tail padding reuse (sizeof=16): "
              << (sizeof(DerivedTailReuse) == 16 ? "PASS" : "FAIL") << "\n";
    std::cout << "[Layout] POD tail padding preserved (sizeof=24): "
              << (sizeof(DerivedPodNoReuse) == 24 ? "PASS" : "FAIL") << "\n";

    MultiDerived obj;
    Base1* b1 = &obj;
    Base2* b2 = &obj;
    uintptr_t u_obj = reinterpret_cast<uintptr_t>(&obj);
    uintptr_t u_b1  = reinterpret_cast<uintptr_t>(b1);
    uintptr_t u_b2  = reinterpret_cast<uintptr_t>(b2);

    ptrdiff_t b1_off = u_b1 - u_obj;
    ptrdiff_t b2_off = u_b2 - u_obj;
    std::cout << "[Layout] Base1 primary subobject offset: " << b1_off << "\n";
    std::cout << "[Layout] Base2 secondary subobject offset: " << b2_off << "\n";
    std::cout << "[Layout] EmptyTag base shares the complete object address: "
              << (static_cast<EmptyTag*>(&obj) == reinterpret_cast<EmptyTag*>(&obj) ? "PASS" : "FAIL") << "\n";

    // Section 2: Vtable Negative Offsets (offset-to-top and RTTI)
    void** vptr_primary = *reinterpret_cast<void***>(b1);
    ptrdiff_t primary_ott = reinterpret_cast<ptrdiff_t>(vptr_primary[-2]);
    const std::type_info* primary_rtti = reinterpret_cast<const std::type_info*>(vptr_primary[-1]);

    void** vptr_secondary = *reinterpret_cast<void***>(b2);
    ptrdiff_t secondary_ott = reinterpret_cast<ptrdiff_t>(vptr_secondary[-2]);
    const std::type_info* secondary_rtti = reinterpret_cast<const std::type_info*>(vptr_secondary[-1]);

    std::cout << "[Vtable] Primary vtable offset-to-top: " << primary_ott << "\n";
    std::cout << "[Vtable] Secondary vtable offset-to-top: " << secondary_ott << "\n";
    std::cout << "[Vtable] Primary RTTI demangled: "
              << abi::__cxa_demangle(primary_rtti->name(), nullptr, nullptr, nullptr) << "\n";
    std::cout << "[Vtable] Secondary RTTI matches primary pointer: "
              << (primary_rtti == secondary_rtti ? "PASS" : "FAIL") << "\n";

    // dynamic_cast<void*> using offset-to-top
    void* dcast_void = dynamic_cast<void*>(b2);
    void* manual_void = reinterpret_cast<void*>(u_b2 + secondary_ott);
    std::cout << "[Vtable] dynamic_cast<void*> matches subobject + offset-to-top: "
              << (dcast_void == manual_void && dcast_void == &obj ? "PASS" : "FAIL") << "\n";

    // Section 3: Adjustor Thunk Verification
    uintptr_t this_via_b1 = b1->f1();
    uintptr_t this_via_b2 = b2->f2();
    std::cout << "[Thunk] Call via Base1* this matches complete object: "
              << (this_via_b1 == u_obj ? "PASS" : "FAIL") << "\n";
    std::cout << "[Thunk] Call via Base2* this adjusted from +16 to complete object: "
              << (this_via_b2 == u_obj && this_via_b2 != u_b2 ? "PASS" : "FAIL") << "\n";

    // Direct invocation through secondary vtable slot (slot 2 is f2, following 2 dtor slots)
    using FuncPtr = uintptr_t (*)(void*);
    FuncPtr thunk_fn = reinterpret_cast<FuncPtr>(vptr_secondary[2]);
    uintptr_t this_via_raw_thunk = thunk_fn(b2);
    std::cout << "[Thunk] Raw thunk call adjusted pointer: "
              << (this_via_raw_thunk == u_obj ? "PASS" : "FAIL") << "\n";

    // Section 4: Diamond Virtual Inheritance and Virtual Base Offsets
    DiamondDerived diam;
    VBase* vb = &diam;
    DiamondB2* db2 = &diam;
    uintptr_t u_diam = reinterpret_cast<uintptr_t>(&diam);
    uintptr_t u_vb   = reinterpret_cast<uintptr_t>(vb);
    uintptr_t u_db2  = reinterpret_cast<uintptr_t>(db2);

    void** vptr_diam_b1 = *reinterpret_cast<void***>(&diam);
    ptrdiff_t vbase_off_b1 = reinterpret_cast<ptrdiff_t>(vptr_diam_b1[-3]);

    void** vptr_diam_b2 = *reinterpret_cast<void***>(db2);
    ptrdiff_t vbase_off_b2 = reinterpret_cast<ptrdiff_t>(vptr_diam_b2[-3]);

    std::cout << "[VBase] Primary vbase_offset value: " << vbase_off_b1 << "\n";
    std::cout << "[VBase] Primary vbase_offset matches (VBase - Diamond): "
              << (u_diam + vbase_off_b1 == u_vb ? "PASS" : "FAIL") << "\n";
    std::cout << "[VBase] Secondary vbase_offset value: " << vbase_off_b2 << "\n";
    std::cout << "[VBase] Secondary vbase_offset matches (VBase - DiamondB2): "
              << (u_db2 + vbase_off_b2 == u_vb ? "PASS" : "FAIL") << "\n";

    // Virtual thunk: VBase::vthis is overridden in DiamondDerived, so calling it through VBase* must
    // subtract the VBase-to-complete-object distance, which is only known at run time (vcall offset).
    std::cout << "[VThunk] Call via VBase* this adjusted to complete object: "
              << (vb->vthis() == u_diam && u_vb != u_diam ? "PASS" : "FAIL") << "\n";

    // Construction vtable: while DiamondB1's constructor ran as a base of DiamondDerived, its vptr pointed
    // to a construction vtable whose RTTI slot names DiamondB1, not the final type.
    void** final_vptr = *reinterpret_cast<void***>(&diam);
    std::cout << "[CtorVtable] vptr seen during base construction differs from final vptr: "
              << (g_b1_ctor_vptr != nullptr && g_b1_ctor_vptr != final_vptr ? "PASS" : "FAIL") << "\n";
    std::cout << "[CtorVtable] RTTI in construction vtable names the base: "
              << (g_b1_ctor_vptr && g_b1_ctor_vptr[-1] == static_cast<const void*>(&typeid(DiamondB1)) ? "PASS" : "FAIL") << "\n";
    std::cout << "[CtorVtable] RTTI in final vtable names the complete type: "
              << (final_vptr[-1] == static_cast<const void*>(&typeid(DiamondDerived)) ? "PASS" : "FAIL") << "\n";

    // Covariant thunk: a call through CovBase* must return a RetB* adjusted to the RetB subobject.
    CovDerived cov;
    CovBase* cb = &cov;
    RetB* via_base = cb->get();
    RetB* direct = static_cast<RetB*>(cov.get());
    std::cout << "[CovThunk] Return pointer adjusted to RetB subobject: "
              << (via_base == direct && reinterpret_cast<uintptr_t>(via_base) != reinterpret_cast<uintptr_t>(&cov.rd) ? "PASS" : "FAIL") << "\n";

    // Section 5: RTTI Structure Inspection (__vmi_class_type_info)
    void* rtti_vptr = *reinterpret_cast<void**>(const_cast<std::type_info*>(primary_rtti));
    Dl_info rtti_dlinfo;
    int dl_status = dladdr(rtti_vptr, &rtti_dlinfo);
    std::cout << "[RTTI] Resolved RTTI metadata class symbol: "
              << (dl_status && rtti_dlinfo.dli_sname ? abi::__cxa_demangle(rtti_dlinfo.dli_sname, 0, 0, 0) : "FAIL")
              << "\n";

    const itanium_abi::__vmi_class_type_info* vmi =
        reinterpret_cast<const itanium_abi::__vmi_class_type_info*>(primary_rtti);
    std::cout << "[RTTI] Direct base count: " << vmi->__base_count << "\n";
    for (unsigned int i = 0; i < vmi->__base_count; ++i) {
        const auto& base = vmi->__base_info[i];
        std::cout << "[RTTI] Base " << i << ": name="
                  << abi::__cxa_demangle(base.__base_type->name(), nullptr, nullptr, nullptr)
                  << ", offset=" << base.offset()
                  << ", public=" << (base.is_public() ? "true" : "false")
                  << ", virtual=" << (base.is_virtual() ? "true" : "false")
                  << "\n";
    }

    // Section 6: Guard variable protocol under contention
    constexpr int kThreads = 8;
    std::atomic<bool> go{false};
    std::atomic<int> wrong_value{0};
    std::vector<std::thread> pool;
    for (int i = 0; i < kThreads; ++i) {
        pool.emplace_back([&] {
            while (!go.load()) { std::this_thread::yield(); }
            if (get_guarded_static() != 42) { wrong_value.fetch_add(1); }
        });
    }
    go.store(true);
    for (auto& t : pool) { t.join(); }
    std::cout << "[Guard] Initializer ran exactly once across 8 racing threads: "
              << (g_init_runs.load() == 1 ? "PASS" : "FAIL") << "\n";
    std::cout << "[Guard] Every thread observed the initialized value: "
              << (wrong_value.load() == 0 ? "PASS" : "FAIL") << "\n";

    return 0;
}
