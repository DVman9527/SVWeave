// Synthetic fixture-only native oracle. No production SVWeave API / pin changes.
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#include "slang/ast/ASTSerializer.h"
#include "slang/ast/ASTVisitor.h"
#include "slang/ast/Compilation.h"
#include "slang/ast/symbols/CompilationUnitSymbols.h"
#include "slang/syntax/SyntaxTree.h"
#include "slang/text/Json.h"
#include "slang/text/SourceManager.h"
#ifndef PCU_STOCK
#include "slang/ast/ExperimentalLeafSession.h"
#endif
using namespace slang;
using namespace slang::ast;
static void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
static std::string fixture(bool b) {
    std::string s =
        "module changed(output bit y); assign y = 1'b0; endmodule\n"
        "module control(output bit y); assign y = 1'b1; endmodule\n"
        "module top(output bit x, x2, z); changed u0(x); changed u2(x2); control u1(z); endmodule\n";
    if (b) s[s.find("1'b0") + 3] = '1';
    return s;
}
static std::string quote(std::string_view s) {
    JsonWriter w; w.writeValue(s); return std::string(w.view());
}
static std::string envelope(std::string ast, bool b) {
    return "{\"ast\":" + ast + ",\"diagnostics\":[],\"active_source\":" + quote(fixture(b)) +
           ",\"logical_path\":\"fixture.sv\",\"macro_provenance\":[],"
           "\"scope\":\"exact-three-module-constant-rhs-fixture-only\"}";
}
static std::string full(bool b) {
    SourceManager sources;
    auto tree = syntax::SyntaxTree::fromText(fixture(b), sources, "fixture.sv");
    Compilation comp;
    comp.addSyntaxTree(tree);
    // Diagnostics cannot be dropped / normalized: any diagnostic rejects this fixture.
    require(comp.getAllDiagnostics().empty(), "full build has diagnostics");
#ifndef PCU_STOCK
    auto diagnosticEmissions = comp.experimentalDiagnosticEmissions;
#endif
    JsonWriter w;
    ASTSerializer serializer(comp, w);
    serializer.setIncludeAddresses(false);
    serializer.setIncludeSourceInfo(true);
    serializer.setDetailedTypeInfo(true);
    w.startObject();
    w.writeProperty("root"); serializer.serialize(comp.getRoot());
    w.writeProperty("concrete_instances"); w.startArray();
    auto visitor = makeVisitor([&](auto& v, const InstanceSymbol& instance) {
        w.startObject();
        w.writeProperty("path"); w.writeValue(instance.getHierarchicalPath());
        w.writeProperty("body"); serializer.serialize(instance.body);
        w.writeProperty("resolved_targets"); w.startArray();
        auto targets = makeVisitor([&](auto&, const NamedValueExpression& expr) {
            w.writeValue(expr.symbol.getHierarchicalPath());
        });
        instance.body.visit(targets);
        w.endArray(); w.endObject(); v.visitDefault(instance);
    });
    comp.getRoot().visit(visitor);
    w.endArray(); w.endObject();
#ifndef PCU_STOCK
    require(comp.experimentalDiagnosticEmissions == diagnosticEmissions, "diagnostic during full serialization");
#endif
    return envelope(std::string(w.view()), b);
}
#ifndef PCU_STOCK
static std::string work(Compilation::ExperimentalWork w) {
    return "{\"expressions\":" + std::to_string(w.expressions) +
           ",\"lookups\":" + std::to_string(w.lookups) +
           ",\"assignments\":" + std::to_string(w.assignments) +
           ",\"conversions\":" + std::to_string(w.conversions) + "}";
}
static void incremental() {
    ExperimentalLeafSession session;
    require(ExperimentalLeafSession::fixture(false) == fixture(false) &&
            ExperimentalLeafSession::fixture(true) == fixture(true), "fixture drift");
    auto a0 = envelope(session.snapshot(0), false);
    require(a0 == full(false), "initial full oracle mismatch");
    auto control0 = session.controlWork();
    auto changed0 = session.changedWork();
    require(control0.expressions > 0 && control0.lookups > 0 &&
            control0.assignments == 1 && control0.conversions > 0, "missing native baseline work");
    std::vector<std::string> rejected;
    auto edit = [&](std::string from, std::string to) {
        auto s = fixture(false); s.replace(s.find(from), from.size(), to); rejected.push_back(s);
    };
    edit("1'b0", "missing"); // negative lookup, rejected BEFORE semantic mutation
    edit("1'b0", "a"); // reference-changing update deliberately unsupported
    edit("1'b0", "2'b00");
    edit("output bit y", "output logic [3:0] y");
    edit("assign y", "bit extra; assign y");
    edit("changed u0(x);", "");
    edit("module changed", "module renamed");
    edit("1'b0", "(");
    for (auto& s : rejected) {
        require(!session.update(s), "unsupported edit admitted");
        require(session.generation() == 0 && envelope(session.snapshot(0), false) == a0,
                "rejected edit changed prior snapshot or diagnostics");
        require(session.changedWork() == changed0 && session.controlWork() == control0,
                "rejected edit performed native semantic work");
    }
    require(session.update(fixture(true)), "A to B denied");
    auto b1 = envelope(session.snapshot(1), true);
    require(b1 == full(true), "B full oracle mismatch");
    auto changed1 = session.changedWork();
    require(changed1.expressions > changed0.expressions && changed1.conversions > changed0.conversions,
            "no native changed-leaf rebinding work");
    require(session.controlRetained() && session.unaffectedWorkRetained() && session.controlWork() == control0,
            "control rebound or lost identity");
    bool stale = false;
    try { (void)session.snapshot(0); } catch (const std::runtime_error&) { stale = true; }
    require(stale, "stale handle accepted");
    require(session.update(fixture(false)), "B to A denied");
    auto a2 = envelope(session.snapshot(2), false);
    require(a2 == full(false) && a2 == a0, "revert oracle mismatch");
    require(session.controlRetained() && session.unaffectedWorkRetained() && session.controlWork() == control0, "control rebound on revert");
    require(!session.update(fixture(true)), "generation bound bypassed");
    require(envelope(session.snapshot(2), false) == a2, "bound rejection changed session");
    auto changed2 = session.changedWork();
    require(changed2.expressions > changed1.expressions && changed2.conversions > changed1.conversions,
            "revert did not rebind");
    std::cout << "{\"status\":\"MICRO_PROTOTYPE_PASS\",\"general_g0\":\"NOT_ESTABLISHED\","
        "\"generations\":[" << a0 << ',' << b1 << ',' << a2 << "],"
        "\"assertions\":{\"full_equivalence\":true,\"stale_handle_rejected\":true,"
        "\"unsupported_edits_rejected\":true,\"rejection_atomic\":true,"
        "\"control_identity_retained\":true,\"control_native_work_unchanged\":true,"
        "\"all_changed_occurrences_updated\":true,\"source_provenance_verified\":true,\"unaffected_native_work_unchanged\":true,\"changed_native_work_observed\":true,\"generation_limit_enforced\":true},"
        "\"counters\":{\"control_before\":" << work(control0) << ",\"control_after\":"
        << work(session.controlWork()) << ",\"changed_before\":" << work(changed0)
        << ",\"changed_after_b\":" << work(changed1) << ",\"changed_after_a\":" << work(changed2)
        << "}}\n";
}
#endif
int main(int argc, char** argv) {
    try {
        if (argc == 3 && std::string(argv[1]) == "--full" &&
            (std::string(argv[2]) == "a" || std::string(argv[2]) == "b")) {
            std::cout << full(std::string(argv[2]) == "b") << '\n'; return 0;
        }
#ifndef PCU_STOCK
        if (argc == 2 && std::string(argv[1]) == "--incremental") { incremental(); return 0; }
#endif
        throw std::runtime_error("usage: --full a|b or fork-only --incremental");
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
