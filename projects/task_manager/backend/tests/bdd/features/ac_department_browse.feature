# ac.department.browse  (requirements: requirement.department_management; operations: operation.department.list)
Feature: Browse Departments succeeds

  Scenario: Browse Departments succeeds
    Given the user is signed in as Manager or Employee
    When List Departments
    Then The Department information is displayed
